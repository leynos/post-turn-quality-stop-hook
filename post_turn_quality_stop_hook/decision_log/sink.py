"""JSON Lines persistence for decision-log records."""

from __future__ import annotations

import json
import os
import threading
import typing as typ

from post_turn_quality_stop_hook.decision_log import platform_support
from post_turn_quality_stop_hook.decision_log.clock import SystemClock, _elapsed_ms
from post_turn_quality_stop_hook.decision_log.constants import (
    LOG_FILE_PREFIX,
    LOG_FILE_SUFFIX,
)
from post_turn_quality_stop_hook.decision_log.ownership import (
    _is_hook_record_descriptor,
)
from post_turn_quality_stop_hook.decision_log.records import _warn
from post_turn_quality_stop_hook.decision_log.retention import prune_old_logs
from post_turn_quality_stop_hook.git import ExcludedPath

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    from post_turn_quality_stop_hook.decision_log.clock import Clock

# Appending must never stall the hook. Another run holds the lock only for one
# short write, so a lock that is still held after this long belongs to
# something else, and the record is dropped rather than waited for.
_LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_RETRY_SECONDS = 0.01
# Failure labels the log may carry. Exception text can name a path, so only a
# class name is kept, and only from this closed set.
_ERROR_CATEGORIES = frozenset({
    "BlockingIOError",
    "FileNotFoundError",
    "IsADirectoryError",
    "NotADirectoryError",
    "OSError",
    "Other",
    "PermissionError",
    "ShortWrite",
    "TimeoutError",
    "UnicodeEncodeError",
    "ValueError",
})


def _error_category(
    error: BaseException | None = None, *, fallback: str = "Other"
) -> str:
    """Return a bounded category for a storage failure."""
    if error is None:
        return fallback
    name = type(error).__name__
    return name if name in _ERROR_CATEGORIES else fallback


class JsonlRecordSink:
    """Append records to one daily JSON Lines file and prune old files.

    Parameters
    ----------
    log_dir
        Directory holding the daily log files.
    clock
        Time source used to choose the daily filename.
    on_error
        Callback for retention failures. Receive a fixed message with no path.
    is_tracked
        Git ownership query. A daily file Git tracks is never appended to,
        because that write would make a clean worktree dirty before the
        uncommitted-changes gate runs and could change the decision. The same
        refusal applies when the query cannot answer. Once the sink refuses a
        write, it stays inert for the rest of the run, including retention.

    """

    def __init__(
        self,
        log_dir: Path,
        *,
        clock: Clock | None = None,
        on_error: cabc.Callable[[str], None] = _warn,
        is_tracked: cabc.Callable[[Path], bool | None] | None = None,
    ) -> None:
        """Store the log directory and collaborators."""
        self._log_dir = log_dir
        self._clock = clock or SystemClock()
        self._on_error = on_error
        self._is_tracked = is_tracked
        self._tracking_checked: set[Path] = set()
        self._writable_paths: set[Path] = set()
        # Exclusions are resolved when the file is created, not when they are
        # queried. A symbolic link that cannot be resolved later would
        # otherwise strand the exclusion outside the worktree and let the run's
        # own log file change the decision.
        self._exclusions: dict[Path, ExcludedPath] = {}
        self._refused = False
        # Storage timings and failure categories stay bounded integers and a
        # short allow-listed label, so the finish record can report how the
        # log behaved without carrying exception text or a path.
        self._writes = 0
        self._write_failures = 0
        self._write_ms = 0
        self._lock_wait_ms = 0
        self._prunes = 0
        self._prune_failures = 0
        self._prune_ms = 0
        self._write_categories: set[str] = set()
        self._prune_categories: set[str] = set()
        # The in-process lock serialises threads, and its owner identifies a
        # same-thread re-entry from a signal handler.
        self._lock = threading.Lock()
        self._lock_owner: int | None = None
        # Once one record has waited the full timeout, the daily file most
        # likely belongs to another process for the rest of this run. Later
        # records try once and give up instead of paying the same wait again.
        self._lock_timed_out = False

    @property
    def directory(self) -> Path:
        """Directory holding this sink's daily log files."""
        return self._log_dir

    def _log_path(self) -> Path:
        """Return today's log file path."""
        date = self._clock.wall().date().isoformat()
        return self._log_dir / f"{LOG_FILE_PREFIX}{date}{LOG_FILE_SUFFIX}"

    def _may_write(self, path: Path) -> bool:
        """Return whether Git permits appending to ``path``.

        A path that Git tracks, or whose tracking status cannot be established,
        refuses the whole run: the start record is written before the Git
        gates, so a single append to a tracked file could change the decision.
        Ownership of an existing file is settled separately, under the lock in
        :meth:`write`, because only there is the check race-free.
        """
        if path in self._tracking_checked:
            return True
        if self._is_tracked is not None:
            try:
                tracked = self._is_tracked(path)
            except Exception:  # ruff:ignore[blind-except]  # an unknown answer must not risk the decision.
                tracked = None
            if tracked is not False:
                self._refused = True
                self._on_error(
                    "the decision log file is tracked by Git; not writing to it"
                    if tracked
                    else "could not determine whether the decision log file is "
                    "tracked by Git; not writing to it"
                )
                return False
        self._tracking_checked.add(path)
        return True

    def _claim(self, descriptor: int, path: Path, *, created: bool) -> bool:
        """Return whether the locked file may receive this run's records.

        The caller holds an exclusive lock, so no other hook process can be
        midway through its first append. A file this run created is ours by
        definition. An empty file is claimed because it holds no content to
        preserve, and a run killed between creating the file and writing its
        first record would otherwise disable logging for the day. Any other
        file must consist solely of hook records: appending to a foreign file,
        or to one that only begins with a hook record, would modify data the
        hook does not own.
        """
        if created or path in self._writable_paths:
            self._writable_paths.add(path)
            return True
        try:
            size = os.fstat(descriptor).st_size
            # Verification streams the content in bounded chunks, so a large
            # genuine daily file is claimed without reading it whole.
            owned = size == 0 or _is_hook_record_descriptor(descriptor)
        except OSError:  # an unreadable file cannot establish ownership
            owned = False
        if owned:
            self._writable_paths.add(path)
            return True
        self._refused = True
        self._on_error(
            "the decision log file was not written by this hook; not writing to it"
        )
        return False

    def excluded_paths(self) -> tuple[ExcludedPath, ...]:
        """Return the daily files this run wrote to, for repository queries.

        Every file this run appended to is listed, including a file an earlier
        invocation created once this run has verified that it consists only of
        hook records. Both kinds of file exist only because logging is enabled,
        so hiding both is what makes a logging-enabled run report the same
        worktree as a logging-disabled one. A file the hook does not own is
        never listed. The set grows during the run, which also covers a run
        that crosses midnight and writes a second daily file.

        Each entry names the directory's physical path, captured when the file
        was created or claimed. Resolving then keeps the exclusion matchable
        even if the symbolic link that led to it cannot be resolved later.
        """
        return tuple(self._exclusions[path] for path in sorted(self._exclusions))

    def _acquire_process_lock(self) -> bool:
        """Take the in-process lock, refusing same-thread re-entry.

        A catchable signal runs its Python handler on the main thread. If the
        handler records an ``interrupted`` finish while that same thread is
        inside :meth:`write`, waiting for the lock would wait on itself for the
        whole timeout. Refusing loses only a record the lock holder is already
        writing, and lets the handler re-raise the signal promptly.
        """
        if self._lock_owner == threading.get_ident():
            return False
        if not self._lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            return False
        self._lock_owner = threading.get_ident()
        return True

    def _release_process_lock(self) -> None:
        """Release the in-process lock taken by :meth:`_acquire_process_lock`."""
        self._lock_owner = None
        self._lock.release()

    def _lock_file(self, descriptor: int) -> bool:
        """Take the cross-process lock, giving up when another run holds it.

        The caller cannot wait indefinitely: the hook must keep running even if
        some other process holds the file. A short bounded retry still absorbs
        the normal case of a concurrent run finishing its single append. The
        wait is accumulated so the finish record can show whether the log was
        contended. After one full wait, later attempts are non-blocking.
        """
        started = self._clock.monotonic()
        deadline = started + _LOCK_TIMEOUT_SECONDS
        try:
            while True:
                try:
                    platform_support.lock_exclusive(descriptor)
                except BlockingIOError:
                    if self._lock_timed_out:
                        return False
                    if self._clock.monotonic() >= deadline:
                        self._lock_timed_out = True
                        return False
                    self._clock.sleep(_LOCK_RETRY_SECONDS)
                    continue
                return True
        finally:
            self._lock_wait_ms += _elapsed_ms(started, self._clock.monotonic())

    @staticmethod
    def _open(path: Path) -> tuple[int, bool]:
        """Open the daily file, reporting whether this run created it.

        Creation is detected with ``O_CREAT | O_EXCL`` rather than by size so a
        pre-existing empty file is not mistaken for this run's own. Both cases
        can be excluded from repository queries once ownership is settled: a
        created file is the run's by definition, and a pre-existing one is
        excluded only after every line is verified as a hook record.
        """
        try:
            descriptor = os.open(
                path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            return os.open(path, os.O_RDWR | os.O_APPEND), False
        return descriptor, True

    def write(self, record: cabc.Mapping[str, object]) -> None:
        """Append one record with a single ``O_APPEND`` write."""
        started = self._clock.monotonic()
        self._writes += 1
        line = json.dumps(dict(record), ensure_ascii=False, separators=(",", ":"))
        lock_started = self._clock.monotonic()
        acquired = self._acquire_process_lock()
        self._lock_wait_ms += _elapsed_ms(lock_started, self._clock.monotonic())
        if not acquired:
            self._record_write_failure("BlockingIOError")
            self._on_error(
                "the decision log is busy; not writing the record (BlockingIOError)"
            )
            return
        try:
            self._write_locked(line)
        finally:
            self._release_process_lock()
            self._write_ms += _elapsed_ms(started, self._clock.monotonic())

    def _record_write_failure(self, category: str) -> None:
        """Count one failed write and remember its bounded category."""
        self._write_failures += 1
        self._write_categories.add(category)

    def storage_stats(self) -> dict[str, object]:
        """Return bounded timings and failure categories for the finish record.

        The numbers describe storage only. They let an operator tell a slow or
        contended log from a failing one without any exception text, path, or
        command output entering the record.
        """
        return {
            "write": {
                "operation": "write",
                "attempts": self._writes,
                "failures": self._write_failures,
                "storage_ms": self._write_ms,
                "lock_wait_ms": self._lock_wait_ms,
                "error_categories": sorted(self._write_categories),
            },
            "prune": {
                "operation": "prune",
                "attempts": self._prunes,
                "failures": self._prune_failures,
                "storage_ms": self._prune_ms,
                "lock_wait_ms": 0,
                "error_categories": sorted(self._prune_categories),
            },
        }

    def _write_locked(self, line: str) -> None:
        """Append ``line`` while this process holds the in-process lock."""
        try:
            path = self._log_path()
            if self._refused or not self._may_write(path):
                return
            try:
                resolved_directory = path.parent.resolve()
            except OSError:
                # The exclusion must name the physical directory, because Git
                # reports physical paths. Without it this run's own log could
                # become visible to the gates, so the record is refused before
                # anything is created and the decision stays independent of
                # logging.
                self._refused = True
                self._record_write_failure("OSError")
                self._on_error(
                    "could not resolve the decision log directory; not writing "
                    "the record (OSError)"
                )
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, created = self._open(path)
            try:
                if created:
                    # Record creation before taking the lock. If another run
                    # holds the file and this one gives up, the empty file it
                    # has just created is still its own and must stay excluded
                    # from repository queries; otherwise its own gates would
                    # see a new untracked file that logging caused.
                    self._exclusions[path] = ExcludedPath(resolved_directory, path.name)
                # Serialise the ownership check with the first append. Without
                # the lock, a concurrent run could see this run's file while it
                # is still empty or only partly written and mistake it for a
                # foreign file.
                if not self._lock_file(descriptor):
                    self._record_write_failure("BlockingIOError")
                    self._on_error(
                        "the decision log is busy; not writing the record "
                        "(BlockingIOError)"
                    )
                    return
                if not self._claim(descriptor, path, created=created):
                    return
                # Ownership is settled now. A pre-existing file that passed the
                # check holds only hook records, so it is a logging artefact
                # exactly as a created file is. Excluding it keeps the run's
                # decision identical to a logging-disabled run's from the
                # second invocation onward, and after a midnight rollover.
                self._exclusions.setdefault(
                    path, ExcludedPath(resolved_directory, path.name)
                )
                # The mode above applies only to a new file. Tighten an existing
                # one too, so a log created by an older version or another
                # process cannot stay readable by other users.
                platform_support.tighten_permissions(descriptor)
                data = (line + "\n").encode("utf-8")
                # Do not retry a short write: a second append could interleave
                # with another hook's record. The warning cannot repair the
                # truncated line, but it makes the failure visible.
                if os.write(descriptor, data) != len(data):
                    self._record_write_failure("ShortWrite")
                    self._on_error(
                        "could not write the complete decision log record (ShortWrite)"
                    )
            finally:
                os.close(descriptor)
        except (OSError, UnicodeEncodeError) as exc:
            # A path or command value decoded with ``surrogateescape`` cannot be
            # encoded as UTF-8. The record is lost, but the decision is not.
            # The warning stays fixed: exception text would carry the state
            # directory and other personal paths into standard error. Only the
            # allow-listed class name is added.
            category = _error_category(exc)
            self._record_write_failure(category)
            self._on_error(f"could not write the decision log ({category})")

    def prune(self, today: dt.date, retention_days: int) -> None:
        """Prune expired daily files without affecting hook behaviour."""
        if self._refused:
            # The sink refused to write because a daily file is tracked. Leave
            # the directory exactly as it was rather than mixing a retention
            # pass into a run that is not allowed to touch its log.
            return
        self._prunes += 1
        started = self._clock.monotonic()
        try:
            prune_old_logs(
                self._log_dir,
                retention_days,
                today=today,
                on_error=self._prune_failed,
                is_tracked=self._is_tracked,
            )
        finally:
            self._prune_ms += _elapsed_ms(started, self._clock.monotonic())

    def _prune_failed(self, message: str) -> None:
        """Count a retention failure and append its bounded category."""
        self._prune_failures += 1
        category = "OSError"
        self._prune_categories.add(category)
        self._on_error(f"{message} ({category})")
