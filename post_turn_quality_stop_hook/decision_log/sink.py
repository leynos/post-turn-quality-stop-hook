"""JSON Lines persistence for decision-log records.

The sink composes the parts an append or a prune needs: a policy that says
which files may be used, a locked appender that performs the one ``O_APPEND``
write, and the Git ownership queries retention asks. Each part can be replaced
on its own, so the policy can be tested without a file and the appender without
a policy.

The sink owns no telemetry: it appends and prunes validated records, returns
the time an append waited on its locks, and lets a storage failure escape. The
:class:`~.telemetry.MeasuredRecordSink` wrapper measures those calls, so a test
can substitute either the storage or the measurement without standing in for
the other.
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
from post_turn_quality_stop_hook.decision_log.records import (
    PruneReport,
    RecordTooLargeError,
)
from post_turn_quality_stop_hook.decision_log.retention import prune_old_logs

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


def _encode(record: cabc.Mapping[str, object]) -> str:
    """Serialise one record, refusing one that exceeds the size bound.

    A record over the bound would make the whole daily file foreign to every
    later run and to retention, so the one record is dropped instead. Dropping
    it cannot change the decision, which is already computed.
    """
    line = dump_record(record)
    if len(line.encode("utf-8")) <= _MAX_RECORD_WRITE_BYTES:
        return line
    message = "record exceeds the writer's per-line bound"
    raise RecordTooLargeError(message)


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

    This unit performs storage only. It appends a validated record through the
    injected appender and policy, and prunes expired files through the injected
    ownership queries. It keeps no timings and counts no failures: the
    :class:`~.telemetry.MeasuredRecordSink` wrapper owns that accounting, so
    each can be substituted without the other.

    Parameters
    ----------
    log_dir
        Directory holding the daily log files.
    clock
        Time source used to choose the daily filename. It is required, so the
        sink reads no process clock behind its caller's back.
    on_error
        Callback for retention failures. It receives a fixed message with no
        path. It is required for the same reason: only a composition root
        binds a warning channel.
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
    appender
        Locked append adapter. It is a storage dependency, supplied by the
        composition root; when omitted, one is built over this sink's clock.
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
    Every collaborator is required, so a sink cannot be constructed with a
    silent default that may be more or less permissive than the caller meant,
    and it never reads the process clock or a warning stream itself.
    :mod:`post_turn_quality_stop_hook.decision_log.composition` is the one named
    place that binds the real Git queries, the system clock, and the
    standard-error writer.

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
        clock: Clock,
        on_error: cabc.Callable[[str], None],
        appender: LockedJsonlAppender | None = None,
        policy: WritePolicy | None = None,
    ) -> None:
        """Store the log directory and assemble the write path."""
        self._log_dir = log_dir
        self._clock = clock
        self._on_error = on_error
        self._is_tracked = is_tracked
        self._is_tracked_entry = is_tracked_entry
        self._in_worktree = in_worktree
        self._is_ignored = is_ignored
        self._appender = appender or LockedJsonlAppender(
            self._clock,
            ownership=DescriptorOwnership(),
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

    def write(self, record: cabc.Mapping[str, object]) -> int:
        """Append one validated record, returning its lock wait in milliseconds.

        A record that cannot be encoded is refused with
        :class:`~.records.RecordTooLargeError`, and a storage failure escapes as
        ``OSError``. The measurement wrapper converts either into a counted,
        fixed warning, so this unit reports nothing itself.
        """
        return self._append_line(_encode(record))

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

    def prune(self, today: dt.date, retention_days: int) -> PruneReport:
        """Prune expired daily files without affecting hook behaviour.

        The sweep reports a scan or deletion failure through this sink's
        warning channel and keeps going, so a retention problem never changes
        the decision. The report counts those messages for the measurement
        wrapper, which records the attempt and its duration around this call.
        """
        if self._policy.refused:
            # The policy refused the log location, so this run may not write.
            # Leave the directory exactly as it was rather than mixing a
            # retention pass into a run that is not allowed to touch its log.
            return PruneReport()
        failures = 0

        def report(message: str) -> None:
            """Warn once and count the failure for the measurement wrapper."""
            nonlocal failures
            failures += 1
            self._on_error(message)

        # Retention owns four ownership questions, so the sink binds the same
        # two queries it was given for writes and lets the composition factory
        # supply the entry and worktree queries.
        removed = prune_old_logs(
            self._log_dir,
            retention_days,
            today=today,
            on_error=report,
            is_tracked=self._is_tracked,
            is_tracked_entry=self._is_tracked_entry,
            in_worktree=self._in_worktree,
            is_ignored=self._is_ignored,
        )
        return PruneReport(removed=removed, failures=failures)
