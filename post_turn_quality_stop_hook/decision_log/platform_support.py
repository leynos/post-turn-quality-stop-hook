"""The platform differences the decision log's sink must bridge.

The package ships wheels for POSIX and Windows, and the two do not offer the
same primitives for a cross-process lock or for descriptor permissions. This
module is the one place that knows which is in use, so the sink's write path
stays identical on both. Each function has the same contract whatever the
platform.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Directory-relative entry operations are what binds a write or a deletion to
# the directory that was checked. Windows has no equivalent, so callers fall
# back to the pathname and its weaker guarantee.
_DIRECTORY_FLAG = getattr(os, "O_DIRECTORY", 0)
_HAS_DIR_FD = os.open in os.supports_dir_fd
# The kernel exposes open descriptors as a directory of symbolic links here.
# Building an entry's pathname on that directory pins it to the descriptor, so
# replacing an ancestor after the descriptor was obtained cannot redirect it.
_FD_ROOTS = tuple(
    Path(root) for root in ("/proc/self/fd", "/dev/fd") if Path(root).is_dir()
)

if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def lock_exclusive(descriptor: int) -> None:
        """Take an exclusive lock on the file, refusing to wait.

        Windows locks a byte range measured from the current position. The
        first byte is the token, and the offset is reset first so a previous
        verification read cannot shift the region.
        """
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(exc.errno or 0, "the decision log is locked") from exc

else:
    import fcntl

    def lock_exclusive(descriptor: int) -> None:
        """Take an exclusive lock on the file, refusing to wait."""
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def tighten_permissions(descriptor: int) -> None:
    """Restrict an open log file to its owner where the platform allows it.

    Windows has no descriptor-level mode call and inherits the state
    directory's access control instead, so there is nothing to tighten there.
    """
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)


def supports_directory_handles() -> bool:
    """Return whether entries can be created relative to a directory handle."""
    return _HAS_DIR_FD


def open_directory(path: Path) -> int:
    """Open ``path`` for entry-relative operations.

    The caller closes the descriptor. Opening the directory once and creating
    the daily file relative to it is what keeps a later replacement of a parent
    pathname from redirecting the creation that was just validated.
    """
    return os.open(path, os.O_RDONLY | _DIRECTORY_FLAG)


def anchored_path(directory: int, name: str) -> Path | None:
    """Return a pathname that reaches ``name`` through ``directory``.

    The operating system resolves the descriptor to the directory that was
    opened, so the returned path describes that physical directory even if a
    parent pathname is replaced afterwards. ``None`` means this platform offers
    no filesystem view of open descriptors; callers then keep the original
    pathname and its weaker guarantee.
    """
    if not _HAS_DIR_FD or not _FD_ROOTS:
        return None
    return _FD_ROOTS[0] / str(directory) / name
