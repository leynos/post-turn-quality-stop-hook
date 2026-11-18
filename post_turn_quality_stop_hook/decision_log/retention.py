"""Retention for the hook's daily decision-log files.

Retention deletes only files the hook can prove it owns, and only when the
deletion is invisible to the gates a run evaluates. An expired file is removed
when Git confirms it is untracked and it either lies outside every worktree or
is one Git confirms it ignores.
"""

from __future__ import annotations

import datetime as dt
import functools
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
    return _remove_expired(candidates, cutoff, on_error, can_delete)


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
    on_error: cabc.Callable[[str], None] | None,
    can_delete: cabc.Callable[[Path], bool],
) -> int:
    """Delete expired daily files the hook owns outside every worktree."""
    removed = 0
    for path in candidates:
        file_date = _log_file_date(path)
        if file_date is None or file_date >= cutoff:
            continue
        if not can_delete(path):
            continue
        try:
            path.unlink()
        except OSError:
            if on_error is not None:
                on_error("could not remove an expired decision-log file")
            continue
        removed += 1
    return removed
