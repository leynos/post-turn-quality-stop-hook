"""File-system adapter for the failure breaker's ``StreakStore`` port.

The count lives under ``$XDG_STATE_HOME/post-turn-quality-stop-hook/`` because
it is machine-local, non-essential state that must survive between hook
processes but does not belong in a repository or the user's configuration.

This module reports failures; it does not decide what they mean. Storage
errors surface as ``StreakStoreError`` and the breaker policy in
``failure_breaker`` chooses to fail open.
"""

from __future__ import annotations

import json
import tempfile
import typing as typ
from pathlib import Path

from post_turn_quality_stop_hook.config import is_non_negative_int
from post_turn_quality_stop_hook.failure_breaker import StreakStoreError, streak_key

if typ.TYPE_CHECKING:
    import collections.abc as cabc

XDG_STATE_SUBPATH = Path("post-turn-quality-stop-hook") / "failure-streaks"


def state_root(environ: cabc.Mapping[str, str], home: Path) -> Path:
    """Return the directory that holds per-directory failure streaks.

    The XDG Base Directory specification requires implementations to ignore
    an empty or relative ``XDG_STATE_HOME`` and fall back to
    ``$HOME/.local/state``. The caller supplies the environment and home
    directory, so this function reads no process state.
    """
    state_home = environ.get("XDG_STATE_HOME", "")
    if state_home and Path(state_home).is_absolute():
        return Path(state_home) / XDG_STATE_SUBPATH
    return home / ".local" / "state" / XDG_STATE_SUBPATH


def streak_path(directory: Path, root: Path) -> Path:
    """Return the streak file for ``directory`` under ``root``."""
    return root / f"{streak_key(directory)}.json"


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
        message = f"no valid consecutive_failures count in {path}"
        raise ValueError(message)
    return count


def write_streak(path: Path, directory: Path, count: int) -> None:
    """Atomically record ``count`` consecutive failures for ``directory``.

    Writing to a sibling temporary file and renaming it means a concurrent
    reader sees either the old or the new count, never a torn file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"directory": str(directory), "consecutive_failures": count})
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as handle:
        handle.write(payload)
    temporary = Path(handle.name)
    try:
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


class FileStreakStore:
    """Store one small JSON file per directory under ``root``."""

    def __init__(self, root: Path) -> None:
        """Keep streak files under ``root``, creating it on first save."""
        self.root = root

    def load(self, directory: Path) -> int:
        """Return the recorded count for ``directory``, or 0 when absent."""
        path = streak_path(directory, self.root)
        try:
            return read_streak(path)
        except (OSError, ValueError) as exc:
            message = f"cannot read {path}: {exc}"
            raise StreakStoreError(message) from exc

    def save(self, directory: Path, count: int) -> None:
        """Record ``count`` consecutive blocks for ``directory``."""
        path = streak_path(directory, self.root)
        try:
            write_streak(path, directory, count)
        except OSError as exc:
            message = f"cannot write {path}: {exc}"
            raise StreakStoreError(message) from exc

    def clear(self, directory: Path) -> None:
        """Delete the streak file for ``directory`` if there is one."""
        path = streak_path(directory, self.root)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            message = f"cannot clear {path}: {exc}"
            raise StreakStoreError(message) from exc
