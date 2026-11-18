"""Retention for the hook's daily decision-log files.

Retention deletes only files the hook can prove it owns, and only when the
deletion is invisible to the gates a run evaluates. An expired file is removed
when Git confirms it is untracked and it either lies outside every worktree or
is one Git confirms it ignores.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import functools
import os
import typing as typ

from post_turn_quality_stop_hook.decision_log.ownership import (
    _is_hook_record,
    _log_file_date,
)
from post_turn_quality_stop_hook.git import is_ignored as git_is_ignored
from post_turn_quality_stop_hook.git import is_in_worktree

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

# Directory-relative entry operations close the window between deciding that a
# name is an expired log the hook owns and removing that name. Windows has no
# equivalent, so it falls back to the path and keeps the identity check.
_DIRECTORY_FLAG = getattr(os, "O_DIRECTORY", 0)
_HAS_DIR_FD = os.unlink in os.supports_dir_fd


def prune_old_logs(  # ruff:ignore[too-many-arguments]  # directory, window, date, error sink and ownership query are distinct inputs.
    log_dir: Path,
    retention_days: int,
    *,
    today: dt.date,
    on_error: cabc.Callable[[str], None] | None = None,
    is_tracked: cabc.Callable[[Path], bool | None] | None = None,
    in_worktree: cabc.Callable[[Path], bool | None] | None = None,
    is_ignored: cabc.Callable[[Path], bool | None] | None = None,
) -> int:
    """Remove daily log files older than the retention period.

    Parameters
    ----------
    log_dir
        Directory containing ``hook-YYYY-MM-DD.jsonl`` files.
    retention_days
        Number of days to retain. Zero keeps only today's file.
    today
        Current date. The caller supplies it from the run's clock, so
        retention has no hidden wall-clock dependency of its own.
    on_error
        Callback for scan and deletion failures. It receives a fixed message
        with no filesystem path, because the log must not expose one.
    is_tracked
        Callback reporting whether Git tracks a file, or ``None`` when that is
        unknown. Anything but a confirmed ``False`` is preserved: deleting a
        tracked file would change the worktree, and the decision log must
        never do that.
    in_worktree
        Callback reporting whether a worktree contains a file, or ``None``
        when that is unknown. A file inside a worktree is preserved unless Git
        also confirms it is ignored: deleting an untracked file would change
        what the uncommitted-changes gate sees, while a logging-disabled run
        would still report it. Defaults to
        :func:`post_turn_quality_stop_hook.git.is_in_worktree`.
    is_ignored
        Callback reporting whether Git ignores a file, or ``None`` when that is
        unknown. Only a confirmed ``True`` permits deleting inside a worktree,
        because ignored paths never appear in either repository query, so the
        deletion is invisible to both runs. Defaults to
        :func:`post_turn_quality_stop_hook.git.is_ignored`.

    Returns
    -------
    int
        Number of files removed. Pruning failures never affect the hook
        decision; they are reported through ``on_error`` so expired files that
        accumulate remain visible.

    """
    cutoff = _retention_cutoff(retention_days, today)
    if cutoff is None:
        return 0
    candidates = _scan_log_dir(log_dir, on_error)
    if candidates is None:
        return 0
    # Bind the ownership rules once so the deletion loop takes a single
    # predicate rather than growing a parameter per rule.
    can_delete = functools.partial(
        _is_prunable,
        is_tracked=is_tracked,
        in_worktree=in_worktree or is_in_worktree,
        is_ignored=is_ignored or git_is_ignored,
    )
    with _opened_directory(log_dir) as directory:
        remove = functools.partial(
            _remove_verified,
            on_error=on_error,
            can_delete=can_delete,
            directory=directory,
        )
        return _remove_expired(candidates, cutoff, remove)


@contextlib.contextmanager
def _opened_directory(log_dir: Path) -> cabc.Iterator[int | None]:
    """Open ``log_dir`` so entry operations can be relative to it.

    A directory that cannot be opened is reported as ``None``: the deletion
    loop then works from full paths, which keeps the identity check but loses
    the guarantee that the entry belongs to the directory that was scanned.
    """
    try:
        descriptor = os.open(log_dir, os.O_RDONLY | _DIRECTORY_FLAG)
    except OSError:
        yield None
        return
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _retention_cutoff(retention_days: int, today: dt.date) -> dt.date | None:
    """Return the oldest date to keep, or ``None`` when nothing is pruned."""
    if retention_days < 0:
        return None
    try:
        return today - dt.timedelta(days=retention_days)
    except OverflowError:
        # A retention period beyond the calendar range keeps every file; the
        # hook must not abort merely because the configured window is huge.
        return None


def _scan_log_dir(
    log_dir: Path,
    on_error: cabc.Callable[[str], None] | None,
) -> list[Path] | None:
    """List the log directory, reporting a scan failure without its path."""
    try:
        return list(log_dir.iterdir())
    except OSError:
        if on_error is not None:
            on_error("could not scan the decision-log directory")
        return None


def _confirms(
    query: cabc.Callable[[Path], bool | None], path: Path, *, expected: bool
) -> bool:
    """Return whether ``query`` gives the expected answer about ``path``.

    Every caller uses this to decide whether deleting a file is safe, so an
    exception or any other answer - including ``None`` - must not be read as
    permission.
    """
    try:
        return query(path) is expected
    except Exception:  # ruff:ignore[blind-except]  # an unknown answer preserves the file.
        return False


def _is_prunable(
    path: Path,
    is_tracked: cabc.Callable[[Path], bool | None] | None,
    in_worktree: cabc.Callable[[Path], bool | None],
    is_ignored: cabc.Callable[[Path], bool | None],
) -> bool:
    """Return whether retention may delete ``path``.

    A name and an age are not evidence that this file belongs to the hook.
    Requiring the producer's own record shape keeps unrelated files safe, and
    a confirmed position outside every worktree keeps the deletion invisible
    to the gates this run is about to evaluate. A logging-disabled run leaves
    an untracked file in place and can block on it; removing it here would
    turn that block into an allow.

    A file inside a worktree is therefore preserved even when untracked, with
    one bounded exception: a file Git confirms it ignores. Ignored paths never
    appear in the changed-file query or the uncommitted-changes gate, so
    deleting one is invisible to both runs and keeps retention bounded when
    the state directory happens to sit inside a repository.
    """
    if not _is_hook_record(path):
        return False
    try:
        inside = in_worktree(path)
    except Exception:  # ruff:ignore[blind-except]  # an unknown answer preserves the file.
        return False
    if inside is None:
        return False
    if inside:
        return _confirms(is_ignored, path, expected=True)
    if is_tracked is None:
        return True
    return _confirms(is_tracked, path, expected=False)


def _remove_expired(
    candidates: cabc.Iterable[Path],
    cutoff: dt.date,
    remove: cabc.Callable[[Path], bool],
) -> int:
    """Delete expired daily files the hook owns outside every worktree.

    ``remove`` performs the verified deletion of one candidate and reports
    whether it happened, so this loop owns only the age window and the count.
    """
    removed = 0
    for path in candidates:
        file_date = _log_file_date(path)
        if file_date is None or file_date >= cutoff:
            continue
        if not remove(path):
            continue
        removed += 1
    return removed


def _remove_verified(
    path: Path,
    on_error: cabc.Callable[[str], None] | None,
    *,
    can_delete: cabc.Callable[[Path], bool],
    directory: int | None,
) -> bool:
    """Delete one verified entry, returning whether it was removed.

    The file that is verified must be the file that is removed. The entry's
    identity is read before and after verification, and only an unchanged entry
    is unlinked, so a name swapped while its content was being read is
    preserved rather than deleted on the strength of the old content.

    Checking and unlinking are separate operations on a directory entry, so a
    replacement in the instant between the second identity check and the
    unlink is outside this guarantee: ``unlink`` has no compare-and-delete
    form. The sweep only touches the hook's own owner-only daily files in its
    state directory, and another hook never renames entries there, so that
    interval is not reachable by supported concurrent use.
    """
    before = _entry_identity(path, directory)
    if before is None or not can_delete(path):
        return False
    if _entry_identity(path, directory) != before:
        return False
    try:
        _unlink(path, directory)
    except OSError:
        if on_error is not None:
            on_error("could not remove an expired decision-log file")
        return False
    return True


def _entry_identity(path: Path, directory: int | None) -> tuple[int, int] | None:
    """Return the device and inode of the directory entry ``path`` names.

    A directory-relative stat cannot be redirected by replacing a parent
    directory, so it is preferred wherever the platform supports it.
    """
    try:
        if directory is not None and _HAS_DIR_FD:
            info = os.stat(path.name, dir_fd=directory)
        else:
            info = path.stat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _unlink(path: Path, directory: int | None) -> None:
    """Remove ``path``, relative to the opened directory where possible."""
    if directory is not None and _HAS_DIR_FD:
        os.unlink(path.name, dir_fd=directory)
        return
    path.unlink()
