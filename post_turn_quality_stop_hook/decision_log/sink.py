"""JSON Lines persistence for decision-log records.

The sink only composes the parts a write needs: a policy that says which
files may be used, a locked appender that performs the one ``O_APPEND`` write,
and a telemetry collector that counts what happened. Each part can be replaced
on its own, so the policy can be tested without a file and the appender without
a policy.

The write itself is split along the questions it answers. A location resolver
answers *where* the record would go, or returns a typed refusal. A persistence
command performs the append at that location. A telemetry wrapper records the
timings and failure categories around either, and is the only part that talks
to the collector.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import typing as typ

from post_turn_quality_stop_hook.decision_log import platform_support
from post_turn_quality_stop_hook.decision_log.append import (
    AppendPolicy,
    LockedJsonlAppender,
)
from post_turn_quality_stop_hook.decision_log.assembly import dump_record
from post_turn_quality_stop_hook.decision_log.clock import SystemClock
from post_turn_quality_stop_hook.decision_log.constants import (
    LOG_FILE_PREFIX,
    LOG_FILE_SUFFIX,
    MAX_RECORD_BYTES,
    RECORD_HEADROOM_BYTES,
)
from post_turn_quality_stop_hook.decision_log.ownership import DescriptorOwnership
from post_turn_quality_stop_hook.decision_log.policy import (
    RefusalReason,
    WritePolicy,
)
from post_turn_quality_stop_hook.decision_log.retention import prune_old_logs
from post_turn_quality_stop_hook.decision_log.stats import StorageStats, error_category
from post_turn_quality_stop_hook.decision_log.timing import _elapsed_ms
from post_turn_quality_stop_hook.diagnostics import warn

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    from post_turn_quality_stop_hook.decision_log.clock import Clock
    from post_turn_quality_stop_hook.decision_log.policy import FileClaim

# The writer keeps the same margin below the reader's limit that the assembly
# uses for the finish record. A record over the bound would make the whole daily
# file foreign to every later run and to retention, so the one record is dropped
# instead; dropping it cannot change the decision, which is already computed.
_MAX_RECORD_WRITE_BYTES = MAX_RECORD_BYTES - RECORD_HEADROOM_BYTES
_OVERSIZED_CATEGORY = "RecordTooLarge"


@contextlib.contextmanager
def _opened_log_directory(directory: Path) -> cabc.Iterator[int | None]:
    """Open the log directory for entry-relative creation.

    The descriptor binds creation to the directory that was validated:
    replacing a parent pathname afterwards cannot redirect the open or the file
    it creates. Platforms without directory-relative opens, and directories
    that cannot be opened, fall back to the pathname and its weaker guarantee.
    """
    if not platform_support.supports_directory_handles():
        yield None
        return
    try:
        descriptor = platform_support.open_directory(directory)
    except OSError:
        yield None
        return
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _anchored_location(path: Path, directory: int | None) -> Path:
    """Return the pathname to create through, anchored when the platform allows.

    The sink still keys the policy by ``path``, the stable daily name. Only the
    operation performed on the filesystem uses the anchored name.
    """
    if directory is None:
        return path
    return platform_support.anchored_path(directory, path.name) or path


@dataclasses.dataclass(slots=True, frozen=True)
class _ResolvedLocation:
    """Where one append would write, once the location questions are answered.

    Attributes
    ----------
    path
        Stable daily name, which is the key the policy caches per file.
    location
        Pathname the append opens: anchored to the validated directory where
        the platform allows it.
    is_absent
        Whether the daily file is absent, so creating it is this append's work
        and the creation question has already been asked.

    """

    path: Path
    location: Path
    is_absent: bool


class _CreatingPolicy(AppendPolicy):
    """Answer the creation question the sink could not ask before the open.

    The sink asks that question only when the daily file is absent, and skips it
    when the file is there. The appender still opens with ``O_CREAT``, so a file
    removed between that existence check and the open is created anew without
    any location question being asked: a visible, untracked file the gates would
    report, which is exactly what the location policy exists to prevent.

    The adapter's policy hook runs after the open, under the lock, and is told
    whether this run created the file. A created file is therefore checked
    before any record is written, through the directory the creation used. A
    refusal removes the file again, because ``O_CREAT`` cannot otherwise be
    undone, and refuses the rest of the run, so retention stays skipped as well.
    """

    def __init__(self, policy: WritePolicy, location: Path) -> None:
        """Wrap ``policy`` for one append made at ``location``."""
        self._policy = policy
        self._location = location

    def claim_append(
        self, path: Path, claim: FileClaim, *, created: bool, location: Path
    ) -> bool:
        """Ask the skipped creation question for a file this append created."""
        if created:
            verdict = self._policy.may_create(self._location)
            if not verdict.permitted:
                if verdict.refusal is not None:
                    self._policy.refuse_run(verdict.refusal)
                # Nothing was written to the file, so it holds nothing this run
                # needs. Leaving it would add the very worktree entry the
                # location policy just refused.
                with contextlib.suppress(OSError):
                    self._location.unlink()
                return False
        return self._policy.claim_append(
            path, claim, created=created, location=location
        )


class JsonlRecordSink:
    """Append records to one daily JSON Lines file and prune old files.

    Parameters
    ----------
    log_dir
        Directory holding the daily log files.
    clock
        Time source used to choose the daily filename and time the locks.
    on_error
        Callback for retention failures. Receive a fixed message with no path.
    is_tracked
        Git ownership query, passed to the default policy. The policy refuses
        a daily file Git tracks, or whose status it cannot establish, because a
        write to one would make a clean worktree dirty before the
        uncommitted-changes gate runs and could change the decision.
    is_visible
        Query for whether a worktree could report the daily file as an
        untracked change, passed to the default policy. Only a definite
        ``False`` permits a write, which is what keeps the log out of every
        decision.
    stats
        Telemetry collector. Defaults to a private one.
    appender
        Locked append adapter. Defaults to one sharing ``clock`` and ``stats``.
    policy
        Ownership and location policy. Defaults to one built from
        ``is_tracked``, ``is_visible`` and ``on_error``.
    is_tracked_entry
        Git query for whether the directory entry itself is tracked, used by
        retention. See
        :func:`post_turn_quality_stop_hook.decision_log.retention.prune_old_logs`.
    in_worktree
        Git query for whether a worktree contains a file, used by retention.
    is_ignored
        Git query for whether Git ignores the directory entry, used by
        retention.

    Notes
    -----
    Every Git query is required, so a sink cannot be constructed with a silent
    default that may be more or less permissive than the caller meant.
    :func:`post_turn_quality_stop_hook.decision_log.composition.default_jsonl_sink`
    is the one named factory that supplies the real Git defaults.

    """

    def __init__(  # ruff:ignore[too-many-arguments]  # each collaborator is an independent seam the tests substitute.
        self,
        log_dir: Path,
        *,
        is_tracked: cabc.Callable[[Path], bool | None],
        is_visible: cabc.Callable[[Path], bool | None],
        is_tracked_entry: cabc.Callable[[Path], bool | None],
        in_worktree: cabc.Callable[[Path], bool | None],
        is_ignored: cabc.Callable[[Path], bool | None],
        clock: Clock | None = None,
        on_error: cabc.Callable[[str], None] = warn,
        stats: StorageStats | None = None,
        appender: LockedJsonlAppender | None = None,
        policy: WritePolicy | None = None,
    ) -> None:
        """Store the log directory and assemble the write path."""
        self._log_dir = log_dir
        self._clock = clock or SystemClock()
        self._on_error = on_error
        self._is_tracked = is_tracked
        self._is_tracked_entry = is_tracked_entry
        self._in_worktree = in_worktree
        self._is_ignored = is_ignored
        self._stats = stats or StorageStats()
        self._appender = appender or LockedJsonlAppender(
            self._clock,
            ownership=DescriptorOwnership(),
            stats=self._stats,
            on_error=on_error,
        )
        self._policy = policy or WritePolicy(
            on_error=on_error, is_tracked=is_tracked, is_visible=is_visible
        )

    @property
    def directory(self) -> Path:
        """Directory holding this sink's daily log files."""
        return self._log_dir

    def _log_path(self) -> Path:
        """Return today's log file path."""
        date = self._clock.wall().date().isoformat()
        return self._log_dir / f"{LOG_FILE_PREFIX}{date}{LOG_FILE_SUFFIX}"

    def storage_stats(self) -> dict[str, dict[str, object]]:
        """Return bounded timings and failure categories for the finish record."""
        return self._stats.snapshot()

    def write(self, record: cabc.Mapping[str, object]) -> None:
        """Append one record, recording its timings and failure categories.

        This is the telemetry wrapper around the append: every attempt is
        counted, however it ends, and a storage failure is reported with a
        fixed, path-free message. The record is lost on failure, but the
        decision is not.
        """
        started = self._clock.monotonic()
        self._stats.start_write()
        lock_wait_ms = 0
        try:
            line = self._encode(record)
            if line is not None:
                lock_wait_ms = self._append_line(line)
        except OSError as exc:
            # An undecodable value is escaped during serialisation, so only a
            # storage failure reaches this handler. The warning stays fixed:
            # exception text would carry the state directory and other personal
            # paths into standard error. Only the allow-listed class name is
            # added.
            category = error_category(exc)
            self._stats.fail_write(category)
            self._on_error(f"could not write the decision log ({category})")
        finally:
            self._stats.add_write(
                storage_ms=_elapsed_ms(started, self._clock.monotonic()),
                lock_wait_ms=lock_wait_ms,
            )

    def _encode(self, record: cabc.Mapping[str, object]) -> str | None:
        """Serialise one record, or drop it when it exceeds the size bound.

        A record over the bound would make the whole daily file foreign to every
        later run and to retention, so the one record is dropped instead.
        Dropping it cannot change the decision, which is already computed.
        """
        line = dump_record(record)
        if len(line.encode("utf-8")) <= _MAX_RECORD_WRITE_BYTES:
            return line
        self._stats.fail_write(_OVERSIZED_CATEGORY)
        self._on_error(f"could not write the decision log ({_OVERSIZED_CATEGORY})")
        return None

    def _append_line(self, line: str) -> int:
        """Append one encoded line, returning the time spent waiting on locks.

        A refused location is reported through the policy, which owns the
        once-per-run warning, and nothing is created or written.
        """
        path = self._log_path()
        # Settling is the caching form of the write query: it asks the
        # tracking question before every append but reuses one visibility
        # answer per daily file, which is why the sink, not the predicate,
        # owns that decision.
        verdict = self._policy.settle_visibility(path)
        if not verdict.permitted:
            if verdict.refusal is not None:
                self._policy.refuse_run(verdict.refusal)
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with _opened_log_directory(path.parent) as directory:
            resolved = self._resolve_location(path, directory)
            if isinstance(resolved, RefusalReason):
                self._policy.refuse_run(resolved)
                return 0
            result = self._appender.append(
                resolved.path,
                line,
                self._append_policy(resolved),
                location=resolved.location,
            )
            return result.lock_wait_ms

    def _resolve_location(
        self, path: Path, directory: int | None
    ) -> _ResolvedLocation | RefusalReason:
        """Return where the append would write, or why it may not.

        An absent daily file is the one append that can add a worktree entry,
        so its location is verified again through the directory that will
        receive it, rather than trusting the earlier answer about the pathname.
        """
        location = _anchored_location(path, directory)
        is_absent = not location.exists()
        if is_absent:
            verdict = self._policy.may_create(location)
            if not verdict.permitted:
                return verdict.refusal or RefusalReason.LOCATION_VISIBLE
        return _ResolvedLocation(path=path, location=location, is_absent=is_absent)

    def _append_policy(self, resolved: _ResolvedLocation) -> AppendPolicy:
        """Return the policy the appender should ask under the lock.

        An absent file was just approved for creation. A present one can still
        vanish before the appender's creating open, and that creation has not
        been checked, so the appender is asked to answer the skipped question
        before it writes anything.
        """
        if resolved.is_absent:
            return self._policy
        return _CreatingPolicy(self._policy, resolved.location)

    def prune(self, today: dt.date, retention_days: int) -> None:
        """Prune expired daily files without affecting hook behaviour."""
        if self._policy.refused:
            # The policy refused the log location, so this run may not write.
            # Leave the directory exactly as it was rather than mixing a
            # retention pass into a run that is not allowed to touch its log.
            return
        self._stats.start_prune()
        started = self._clock.monotonic()
        try:
            # Retention owns four ownership questions, so the sink binds the
            # same two queries it was given for writes and lets the composition
            # factory supply the entry and worktree queries.
            prune_old_logs(
                self._log_dir,
                retention_days,
                today=today,
                on_error=self._prune_failed,
                is_tracked=self._is_tracked,
                is_tracked_entry=self._is_tracked_entry,
                in_worktree=self._in_worktree,
                is_ignored=self._is_ignored,
            )
        finally:
            self._stats.add_prune(
                storage_ms=_elapsed_ms(started, self._clock.monotonic())
            )

    def _prune_failed(self, message: str) -> None:
        """Count a retention failure and append its bounded category."""
        self._stats.fail_prune("OSError")
        self._on_error(f"{message} (OSError)")
