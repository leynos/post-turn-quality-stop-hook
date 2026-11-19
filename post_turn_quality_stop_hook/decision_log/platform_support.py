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
