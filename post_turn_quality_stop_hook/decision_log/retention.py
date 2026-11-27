"""Retention sweep for the hook's daily decision-log files.

Retention deletes only files the hook can prove it owns, and only when the
deletion is invisible to the gates a run evaluates. An expired file is removed
when Git confirms it is untracked and it either lies outside every worktree or
is one Git confirms it ignores.

This module is the filesystem half of retention. The domain half, which decides
*when* the sweep runs once the configured window is known, is
:class:`post_turn_quality_stop_hook.decision_log.schedule.RetentionSchedule`.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import functools
import os
import typing as typ

from post_turn_quality_stop_hook.decision_log import platform_support
from post_turn_quality_stop_hook.decision_log.ownership import (
    _is_hook_record,
    _log_file_date,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

# Directory-relative entry operations close the window between deciding that a
# name is an expired log the hook owns and removing that name. Windows has no
# equivalent, so it falls back to the path and keeps the identity check.
_DIRECTORY_FLAG = getattr(os, "O_DIRECTORY", 0)
_HAS_DIR_FD = os.unlink in os.supports_dir_fd


def prune_old_logs(  # ruff:ignore[too-many-arguments]  # directory, window, date, error sink and the four ownership queries are distinct inputs.
    log_dir: Path,
    retention_days: int,
    *,
    today: dt.date,
    is_tracked: cabc.Callable[[Path], bool | None],
    is_tracked_entry: cabc.Callable[[Path], bool | None],
    in_worktree: cabc.Callable[[Path], bool | None],
    is_ignored: cabc.Callable[[Path], bool | None],
    on_error: cabc.Callable[[str], None] | None = None,
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
    is_tracked
        Callback reporting whether Git tracks a file, or ``None`` when that is
        unknown. Anything but a confirmed ``False`` is preserved: deleting a
        tracked file would change the worktree, and the decision log must
        never do that.
    is_tracked_entry
        Callback reporting whether Git tracks the directory entry itself,
        resolved without following a symbolic link at that name. The
        resolved-target query above would otherwise report the link's target -
        which may be untracked and ignored - and the unlink would remove the
        tracked entry instead.
    in_worktree
        Callback reporting whether a worktree contains a file, or ``None``
        when that is unknown. A file inside a worktree is preserved unless Git
        also confirms it is ignored: deleting an untracked file would change
        what the uncommitted-changes gate sees, while a logging-disabled run
        would still report it.
    is_ignored
        Callback reporting whether Git ignores a file, or ``None`` when that is
        unknown. Only a confirmed ``True`` permits deleting inside a worktree,
        because ignored paths never appear in either repository query, so the
        deletion is invisible to both runs.
    on_error
        Callback for scan and deletion failures. It receives a fixed message
        with no filesystem path, because the log must not expose one.

    Returns
    -------
    int
        Number of files removed. Pruning failures never affect the hook
        decision; they are reported through ``on_error`` so expired files that
        accumulate remain visible.

    Notes
    -----
    All four ownership queries are required, so retention cannot be invoked
    with a silent default that might delete more than the caller intended.
    :func:`post_turn_quality_stop_hook.decision_log.composition.prune_with_defaults`
    is the one named factory that supplies the real Git defaults.

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
        is_tracked_entry=is_tracked_entry,
        in_worktree=in_worktree,
        is_ignored=is_ignored,
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


def _is_untracked(is_tracked: cabc.Callable[[Path], bool | None], path: Path) -> bool:
    """Return whether deleting ``path`` cannot remove a committed file.

    The query runs before either deletion branch. A Git-tracked file is still
    matched by ignore rules, so asking only inside a worktree would let a
    committed daily log that also matches ``.gitignore`` be deleted. Only a
    confirmed untracked answer permits removing the file.
    """
    return _confirms(is_tracked, path, expected=False)


def _is_prunable(  # ruff:ignore[too-many-arguments,too-many-positional-arguments,too-many-return-statements]  # the four ownership queries are independent rules; each failing rule preserves the file.
    path: Path,
    is_tracked: cabc.Callable[[Path], bool | None],
    is_tracked_entry: cabc.Callable[[Path], bool | None],
    in_worktree: cabc.Callable[[Path], bool | None],
    is_ignored: cabc.Callable[[Path], bool | None],
) -> bool:
    """Return whether retention may delete ``path``.

    A name and an age are not evidence that this file belongs to the hook.
    Requiring the producer's own record shape keeps unrelated files safe.
    Deleting a tracked file would change the worktree, so only a confirmed
    untracked answer permits removal anywhere; a tracked or unknown answer
    preserves the file. That check runs before the location question, because
    a committed file can also match an ignore rule.

    The resolved-target question asks about the file a symbolic link points at,
    while the unlink removes the link itself. A tracked link whose target is
    untracked and ignored would therefore pass every later rule, so the entry
    is asked about separately, without following it, and a tracked or unknown
    answer preserves it. That keeps a committed entry in the worktree, which is
    what the uncommitted-changes gate reports.

    Outside every worktree, a confirmed untracked file may go: a
    logging-disabled run cannot see it either, so the deletion cannot change a
    decision. A file inside a worktree is otherwise preserved even when
    untracked, with one bounded exception: a file Git confirms it ignores.
    Ignored paths never appear in the changed-file query or the
    uncommitted-changes gate, so deleting one is invisible to both runs and
    keeps retention bounded when the state directory happens to sit inside a
    repository.

    The ignore question is asked about the directory entry, not about whatever
    a symbolic link at that name points at: ``is_ignored`` resolves only the
    parent, so it names the link rather than its target. Removing the entry is
    what the unlink does, so the entry is what must be ignored; asking about
    the target would let a link Git lists as an uncommitted change be removed
    because its target happens to be ignored.
    """
    if not _is_hook_record(path):
        return False
    if not _is_untracked(is_tracked, path):
        return False
    if not _is_untracked(is_tracked_entry, path):
        return False
    try:
        inside = in_worktree(path)
    except Exception:  # ruff:ignore[blind-except]  # an unknown answer preserves the file.
        return False
    if inside is None:
        return False
    if inside:
        return _confirms(is_ignored, path, expected=True)
    return True


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

    The file that is verified must be the file that is removed. Eligibility is
    therefore checked through the opened directory rather than through the
    pathname: the source inspection and the Git questions both use a name
    anchored to the descriptor, so replacing a parent pathname during the check
    cannot make one directory's valid log authorise deleting another
    directory's unrelated file. The entry's identity is read before and after
    verification, and only an unchanged entry is unlinked, so a name swapped
    while its content was being read is preserved rather than deleted on the
    strength of the old content.

    Checking and unlinking are separate operations on a directory entry, so a
    replacement in the instant between the second identity check and the
    unlink is outside this guarantee: ``unlink`` has no compare-and-delete
    form. The sweep only touches the hook's own owner-only daily files in its
    state directory, and another hook never renames entries there, so that
    interval is not reachable by supported concurrent use.
    """
    if not _binds_to_directory(path, directory):
        return False
    before = _entry_identity(path, directory)
    if before is None or not can_delete(_verification_path(path, directory)):
        return False
    if _entry_identity(path, directory) != before:
        return False
    if not _binds_to_directory(path, directory):
        return False
    try:
        _unlink(path, directory)
    except OSError:
        if on_error is not None:
            on_error("could not remove an expired decision-log file")
        return False
    return True


def _verification_path(path: Path, directory: int | None) -> Path:
    """Return the name eligibility should be checked through.

    The content check and the Git queries take pathnames, while the unlink runs
    relative to the opened directory. Anchoring the name to that descriptor
    makes every check describe the entry that will actually be removed. Without
    a descriptor, or on a platform with no view of open descriptors, the
    original pathname is the only usable answer and the identity checks above
    still guard the removal.
    """
    if directory is None or not _HAS_DIR_FD:
        return path
    return platform_support.anchored_path(directory, path.name) or path


def _binds_to_directory(path: Path, directory: int | None) -> bool:
    """Whether ``path`` is an entry of the opened ``directory`` right now.

    Eligibility checks read the file by pathname, while the unlink runs
    relative to the opened directory. Comparing the directory descriptor's
    identity with the path's parent binds the two: a renamed or replaced
    parent is detected and the entry is preserved. Without a directory
    descriptor, or on a platform without directory-relative operations, both
    the checks and the unlink use the same pathname, so no binding is needed.
    """
    if directory is None or not _HAS_DIR_FD:
        return True
    try:
        opened = os.fstat(directory)
        parent = path.parent.stat()
    except OSError:
        return False
    return (opened.st_dev, opened.st_ino) == (parent.st_dev, parent.st_ino)


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
