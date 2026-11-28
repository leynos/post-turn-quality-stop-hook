"""File-system adapter for the failure breaker's ``StreakStore`` port.

The count lives under ``$XDG_STATE_HOME/post-turn-quality-stop-hook/`` because
it is machine-local, non-essential state that must survive between hook
processes but does not belong in a repository or the user's configuration.

Each streak is one JSON file named by its ``StreakId``. Mutations run under an
exclusive operating-system lock on a sibling ``.lock`` file, so concurrent
hook processes neither lose updates nor both reset one tripped streak. The
kernel releases the lock if a process dies, so a crash cannot wedge the
breaker.

This module reports failures; it does not decide what they mean. Storage
errors surface as ``StreakStoreError`` with the operation and error kind but
no path, and the breaker policy in ``failure_breaker`` chooses to fail open.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import sys
import tempfile
import typing as typ
from pathlib import Path

from post_turn_quality_stop_hook.config import is_non_negative_int
from post_turn_quality_stop_hook.failure_breaker import StreakId, StreakStoreError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

XDG_STATE_SUBPATH = Path("post-turn-quality-stop-hook") / "failure-streaks"

if sys.platform == "win32":  # pragma: no cover - exercised only on Windows
    import msvcrt

    def _acquire(handle: typ.BinaryIO) -> None:
        """Lock the first byte, retrying as ``msvcrt.LK_LOCK`` does."""
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _release(handle: typ.BinaryIO) -> None:
        """Unlock the first byte."""
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _acquire(handle: typ.BinaryIO) -> None:
        """Take an exclusive advisory lock, waiting for any holder."""
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _release(handle: typ.BinaryIO) -> None:
        """Drop the advisory lock."""
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def directory_streak_id(directory: Path) -> StreakId:
    """Return the streak identity for a working directory.

    The SHA-256 keeps the identity short, separator-free and safe to log.
    The filesystem codec round-trips surrogate-escaped bytes from non-UTF-8
    POSIX names, which strict UTF-8 encoding would reject.
    """
    return StreakId(hashlib.sha256(os.fsencode(directory)).hexdigest())


def state_root(environ: cabc.Mapping[str, str], home: cabc.Callable[[], Path]) -> Path:
    """Return the directory that holds per-directory failure streaks.

    The XDG Base Directory specification requires implementations to ignore
    an empty or relative ``XDG_STATE_HOME`` and fall back to
    ``$HOME/.local/state``. The caller supplies the environment and a home
    resolver, so this function reads no process state. ``home`` is called
    only for the fallback, because resolving it can fail, for example with
    ``RuntimeError`` when no home directory is known.
    """
    state_home = environ.get("XDG_STATE_HOME", "")
    if state_home and Path(state_home).is_absolute():
        return Path(state_home) / XDG_STATE_SUBPATH
    return home() / ".local" / "state" / XDG_STATE_SUBPATH


def read_streak(path: Path) -> int:
    """Return the recorded count, or 0 when no streak file exists.

    Raises
    ------
    OSError
        The file exists but cannot be read.
    ValueError
        The file does not hold a non-negative ``consecutive_failures`` count.

    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    data = json.loads(text)
    count = data.get("consecutive_failures") if isinstance(data, dict) else None
    if not is_non_negative_int(count):
        message = "no valid consecutive_failures count"
        raise ValueError(message)
    return count


def write_streak(path: Path, count: int) -> None:
    """Atomically record ``count`` consecutive failures in ``path``.

    Writing to a sibling temporary file and renaming it means a reader that
    takes no lock sees either the old or the new count, never a torn file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as handle:
        handle.write(json.dumps({"consecutive_failures": count}))
    temporary = Path(handle.name)
    try:
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def describe_error(exc: Exception) -> str:
    """Name an error's kind without its message, which may hold a path."""
    if isinstance(exc, OSError) and exc.errno is not None:
        code = errno.errorcode.get(exc.errno, str(exc.errno))
        return f"{type(exc).__name__} ({code})"
    if isinstance(exc, ValueError):
        return "invalid streak record"
    return type(exc).__name__


def erase_streak(path: Path) -> None:
    """Remove the record in ``path``, or zero it in place if it cannot go.

    Deleting needs write access to the directory; overwriting the existing
    file needs only access to the file. Falling back to the overwrite keeps a
    pass or a reset recorded in more of the ways storage can be degraded.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        path.write_text(json.dumps({"consecutive_failures": 0}), encoding="utf-8")


@contextlib.contextmanager
def _store_errors(operation: str) -> cabc.Iterator[None]:
    """Translate storage failures into a path-free ``StreakStoreError``."""
    try:
        yield
    except (OSError, ValueError) as exc:
        message = f"cannot {operation} streak: {describe_error(exc)}"
        raise StreakStoreError(message) from exc


class FileStreakStore:
    """Store one small JSON file per streak under ``root``."""

    def __init__(self, root: Path) -> None:
        """Keep streak files under ``root``, creating it on first write."""
        self.root = root

    def path_for(self, streak: StreakId) -> Path:
        """Return the JSON file that records ``streak``."""
        return self.root / f"{streak}.json"

    @contextlib.contextmanager
    def _locked(self, streak: StreakId) -> cabc.Iterator[Path]:
        """Hold the streak's exclusive lock and yield its record path."""
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / f"{streak}.lock").open("a+b") as handle:
            _acquire(handle)
            try:
                yield self.path_for(streak)
            finally:
                _release(handle)

    def load(self, streak: StreakId) -> int:
        """Return the recorded count for ``streak``, or 0 when absent."""
        with _store_errors("load"):
            return read_streak(self.path_for(streak))

    def increment(self, streak: StreakId) -> int:
        """Add one to the stored count and return it.

        A corrupt record is replaced rather than blocking the count forever.
        """
        with _store_errors("increment"), self._locked(streak) as path:
            try:
                current = read_streak(path)
            except ValueError:
                current = 0
            write_streak(path, current + 1)
            return current + 1

    def clear(self, streak: StreakId) -> None:
        """Delete the record for ``streak`` if there is one."""
        with _store_errors("clear"), self._locked(streak) as path:
            erase_streak(path)

    def reset_if_tripped(self, streak: StreakId, limit: int) -> int:
        """Clear ``streak`` only if it still reaches ``limit``.

        Checking and clearing under one lock means exactly one of several
        concurrent processes that saw the tripped count performs the reset,
        and the count it returns is the one it actually cleared.
        """
        with _store_errors("reset"), self._locked(streak) as path:
            try:
                current = read_streak(path)
            except ValueError:
                current = 0
            if current < limit:
                return 0
            erase_streak(path)
            return current
