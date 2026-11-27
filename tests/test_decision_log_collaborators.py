"""The sink's collaborators are substitutable one at a time.

The storage collector, the locked append adapter and the write policy are
separate seams. Replacing any one of them must leave the others working, so a
test can measure or force one behaviour without standing in for the rest. The
log has the same property: it writes its storage record from the collector it
was given, never by reading telemetry back from the sink.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import typing as typ
from pathlib import Path

from decision_log_support import _FixedClock

from post_turn_quality_stop_hook import decision_log as log_mod

# The concrete sink and appender are adapters; the package exports the
# domain-facing names only.
from post_turn_quality_stop_hook.decision_log import adapters as adapters_mod
from post_turn_quality_stop_hook.decision_log import append as append_mod
from post_turn_quality_stop_hook.decision_log import sink as sink_mod
from post_turn_quality_stop_hook.decision_log.ownership import DescriptorOwnership
from post_turn_quality_stop_hook.diagnostics import warn

_WALL = dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC)
_LOG_NAME = "hook-2026-11-06.jsonl"
# A lock wait the sink never measures, so its route to the collector is visible.
_ADAPTER_LOCK_WAIT_MS = 7
# Two failures let the wrapper's per-failure counting be told from one.
_REPORTED_PRUNE_FAILURES = 2


class _RecordingStats(log_mod.StorageStats):
    """Collector that records the calls the sink makes into it."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
        super().__init__()
        self.calls: list[tuple[str, object]] = []

    def start_write(self) -> None:
        """Record the attempt, then count it."""
        self.calls.append(("start_write", None))
        super().start_write()

    def add_write(self, *, storage_ms: int, lock_wait_ms: int) -> None:
        """Record the timings, then accumulate them."""
        self.calls.append(("add_write", (storage_ms, lock_wait_ms)))
        super().add_write(storage_ms=storage_ms, lock_wait_ms=lock_wait_ms)

    def fail_write(self, category: str) -> None:
        """Record the failure, then count it."""
        self.calls.append(("fail_write", category))
        super().fail_write(category)


class _RefusingPolicy(log_mod.WritePolicy):
    """Policy that refuses every file without touching the disk."""

    def __init__(self) -> None:
        """Supply the required queries even though a refusal asks neither."""
        super().__init__(
            on_error=lambda _message: None,
            is_tracked=lambda _candidate: False,
            is_visible=lambda _candidate: False,
        )

    def permits_write(self, path: Path) -> log_mod.WriteVerdict:
        """Refuse the write, as a tracked daily file would.

        The predicate is read-only: the sink records the refusal through
        :meth:`WritePolicy.refuse_run`, which is what emits the warning.
        """
        del path
        return log_mod.WriteVerdict.denied(log_mod.RefusalReason.FILE_TRACKED)

    def settle_visibility(self, path: Path) -> log_mod.WriteVerdict:
        """Refuse the append, which is the predicate the sink now asks."""
        del path
        return log_mod.WriteVerdict.denied(log_mod.RefusalReason.FILE_TRACKED)


class _RecordingAppender(adapters_mod.LockedJsonlAppender):
    """Append adapter that keeps the call instead of writing."""

    def __init__(self, clock: log_mod.Clock) -> None:
        """Start with no recorded appends."""
        super().__init__(
            clock, ownership=DescriptorOwnership(), on_error=lambda _message: None
        )
        self.calls: list[tuple[Path, str, object, Path | None]] = []

    def append(
        self,
        path: Path,
        line: str,
        policy: append_mod.AppendPolicy,
        *,
        location: Path | None = None,
    ) -> append_mod.AppendResult:
        """Record the request and report a write that waited on the lock."""
        self.calls.append((path, line, policy, location))
        return append_mod.AppendResult(wrote=True, lock_wait_ms=_ADAPTER_LOCK_WAIT_MS)


class _SentinelStats(log_mod.StorageStats):
    """Collector whose report is recognisable at a glance."""

    _SNAPSHOT: typ.ClassVar[dict[str, dict[str, object]]] = {
        "write": {"operation": "write", "attempts": 99}
    }

    @property
    def attempts(self) -> int:
        """Claim one attempt so the log emits a storage record."""
        return 1

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Return the sentinel report instead of measured counters."""
        return self._SNAPSHOT


class _RecordingPruneStorage:
    """Storage unit that records prune requests without touching a file."""

    def __init__(self, *, failures: int = 0) -> None:
        """Start with no recorded prunes and the requested failure count."""
        self.pruned: list[tuple[dt.date, int]] = []
        self._failures = failures

    def write(self, record: typ.Mapping[str, object]) -> int:
        """Accept a record without storing it."""
        del record
        return 0

    def prune(self, today: dt.date, retention_days: int) -> log_mod.PruneReport:
        """Record the request and report the prepared sweep result."""
        self.pruned.append((today, retention_days))
        return log_mod.PruneReport(failures=self._failures)


class _RecordingSink:
    """Sink that keeps records in memory, keeping no telemetry."""

    def __init__(self) -> None:
        """Start with no records."""
        self.records: list[dict[str, object]] = []

    def write(self, record: typ.Mapping[str, object]) -> None:
        """Keep a copy of the written record."""
        self.records.append(dict(record))

    def prune(self, today: dt.date, retention_days: int) -> None:
        """Prune nothing."""


def _storage_sink(
    log_dir: Path,
    *,
    clock: log_mod.Clock,
    appender: adapters_mod.LockedJsonlAppender | None = None,
    policy: log_mod.WritePolicy | None = None,
) -> adapters_mod.JsonlRecordSink:
    """Return the storage unit whose collaborators the caller controls.

    The Git answers describe an untracked file outside every worktree, so the
    default policy and any retention pass permit the temporary directory.
    These tests are about substituting one collaborator, not about Git.
    """
    return adapters_mod.JsonlRecordSink(
        log_dir,
        clock=clock,
        on_error=warn,
        appender=appender,
        policy=policy,
        is_tracked=lambda _candidate: False,
        is_visible=lambda _candidate: False,
        is_tracked_entry=lambda _candidate: False,
        in_worktree=lambda _candidate: False,
        is_ignored=lambda _candidate: False,
    )


def _sink(  # ruff:ignore[too-many-arguments]  # each collaborator the caller may substitute is a distinct parameter.
    log_dir: Path,
    *,
    clock: log_mod.Clock,
    stats: log_mod.StorageStats | None = None,
    appender: adapters_mod.LockedJsonlAppender | None = None,
    policy: log_mod.WritePolicy | None = None,
) -> adapters_mod.MeasuredRecordSink:
    """Return a measured sink whose substituted collaborators the caller controls.

    The storage unit and the measurement wrapper are separate units, and this
    helper wires them the way the composition root does: one clock and one
    collector are shared, and the appender reports its lock failures into the
    same collector.
    """
    resolved_stats = stats or log_mod.StorageStats()
    storage = _storage_sink(
        log_dir,
        clock=clock,
        appender=appender
        or adapters_mod.LockedJsonlAppender(
            clock,
            ownership=DescriptorOwnership(),
            on_failure=resolved_stats.fail_write,
            on_error=warn,
        ),
        policy=policy,
    )
    return adapters_mod.MeasuredRecordSink(
        storage,
        clock=clock,
        stats=resolved_stats,
        on_error=warn,
    )


def test_a_substituted_collector_receives_the_sink_telemetry(tmp_path: Path) -> None:
    """The sink reports into the collector it was given, even when it refuses."""
    log_dir = tmp_path / "log"
    stats = _RecordingStats()
    sink = _sink(
        log_dir, clock=_FixedClock(_WALL, 0.0), stats=stats, policy=_RefusingPolicy()
    )

    sink.write({"record_type": "start"})

    assert [name for name, _payload in stats.calls] == ["start_write", "add_write"], (
        "a refused write must still be counted as an attempt with its timings,"
        f" but the collector saw {stats.calls!r}"
    )
    assert sink.storage_stats() == stats.snapshot(), (
        "the sink must report the substituted collector's view,"
        f" but reported {sink.storage_stats()!r}"
    )
    assert not (log_dir / _LOG_NAME).exists(), (
        "a refused write must not create the daily file"
    )


def test_a_substituted_appender_is_asked_instead_of_the_locked_one(  # ruff:ignore[too-many-locals]  # each collaborator and assertion is a distinct input.
    tmp_path: Path,
) -> None:
    """The sink hands the encoded line to the adapter and keeps its timing."""
    log_dir = tmp_path / "log"
    stats = log_mod.StorageStats()
    clock = _FixedClock(_WALL, 0.0)
    appender = _RecordingAppender(clock)
    policy = log_mod.WritePolicy(
        on_error=lambda _message: None,
        is_tracked=lambda _candidate: False,
        is_visible=lambda _candidate: False,
    )
    sink = _sink(log_dir, clock=clock, stats=stats, appender=appender, policy=policy)
    record = {"record_type": "progress", "event": "command_start"}

    sink.write(record)

    assert len(appender.calls) == 1, (
        f"the substituted adapter must be asked once, but was asked {appender.calls!r}"
    )
    path, line, requested_policy, location = appender.calls[0]
    assert path == log_dir / _LOG_NAME, (
        f"the adapter must receive today's daily file, but received {path}"
    )
    assert location is not None, (
        "the adapter must be given a pathname for the validated directory"
    )
    assert location.name == path.name, (
        f"the anchored pathname must name the daily file, but was {location!r}"
    )
    assert json.loads(line) == record, (
        f"the adapter must receive the encoded record, but received {line!r}"
    )
    assert policy is requested_policy, (
        "the adapter must be handed the sink's own policy,"
        f" but was handed {requested_policy!r}"
    )
    assert stats.snapshot()["write"]["lock_wait_ms"] == _ADAPTER_LOCK_WAIT_MS, (
        "the adapter's lock wait must reach the collector,"
        f" but the collector reported {stats.snapshot()['write']!r}"
    )
    assert not path.exists(), "the substituted adapter must be the only writer"


def test_a_policy_refusal_keeps_the_substituted_appender_idle(
    tmp_path: Path,
) -> None:
    """The policy is consulted before the adapter, so the two stay independent."""
    clock = _FixedClock(_WALL, 0.0)
    stats = log_mod.StorageStats()
    appender = _RecordingAppender(clock)
    sink = _sink(
        tmp_path / "log",
        clock=clock,
        stats=stats,
        appender=appender,
        policy=_RefusingPolicy(),
    )

    sink.write({"record_type": "start"})

    assert appender.calls == [], (
        f"a refused write must never reach the adapter, but it saw {appender.calls!r}"
    )
    assert stats.snapshot()["write"]["attempts"] == 1, (
        "the attempt must still be counted, but the collector reported"
        f" {stats.snapshot()['write']!r}"
    )


def test_the_sink_splits_storage_and_telemetry(tmp_path: Path) -> None:
    """Storage and measurement are independently observable.

    The storage unit answers the location question and performs the one
    append; the measured wrapper counts the attempt and its timings around it.
    This pins the split so a future change cannot fold telemetry back into the
    storage unit.
    """
    log_dir = tmp_path / "log"
    clock = _FixedClock(_WALL, 0.0)
    stats = log_mod.StorageStats()
    appender = _RecordingAppender(clock)
    storage = _storage_sink(log_dir, clock=clock, appender=appender)
    sink = adapters_mod.MeasuredRecordSink(
        storage, clock=clock, stats=stats, on_error=warn
    )

    resolved = storage._resolve_location(log_dir / _LOG_NAME, None)

    assert isinstance(resolved, sink_mod._ResolvedLocation)
    assert resolved.is_absent is True, "the daily file does not exist yet"
    assert appender.calls == [], "resolving a location must not append"
    assert stats.snapshot()["write"]["attempts"] == 0, (
        "resolving a location is storage work, not a measured attempt"
    )

    sink.write({"record_type": "start"})

    assert len(appender.calls) == 1, "the persistence command runs exactly once"
    assert stats.snapshot()["write"]["attempts"] == 1, (
        "the telemetry wrapper counts the attempt it wrapped"
    )


def test_a_substituted_storage_unit_receives_the_prune(
    tmp_path: Path,
) -> None:
    """The wrapper measures the prune it delegates and nothing else.

    A storage unit that keeps no files proves the wrapper does not reach the
    filesystem itself, and the measured attempt shows the prune path is a
    distinct seam from the append path.
    """
    clock = _FixedClock(_WALL, 0.0)
    stats = log_mod.StorageStats()
    storage = _RecordingPruneStorage()
    sink = adapters_mod.MeasuredRecordSink(
        storage, clock=clock, stats=stats, on_error=warn
    )

    sink.prune(dt.date(2026, 11, 6), 7)

    assert storage.pruned == [(dt.date(2026, 11, 6), 7)], (
        f"the wrapper must delegate the prune, but saw {storage.pruned!r}"
    )
    prune_stats = stats.snapshot()["prune"]
    assert prune_stats["attempts"] == 1
    assert prune_stats["failures"] == 0
    assert not (tmp_path / "log").exists(), "the wrapper must write nothing itself"


def test_the_wrapper_counts_the_failures_a_sweep_reports() -> None:
    """A reported sweep failure reaches the collector, not the sink.

    The storage unit counts nothing; it returns how many failures it reported.
    The wrapper turns that report into the bounded category the storage record
    carries, so a retention problem stays visible without the sink owning
    telemetry.
    """
    clock = _FixedClock(_WALL, 0.0)
    stats = log_mod.StorageStats()
    sink = adapters_mod.MeasuredRecordSink(
        _RecordingPruneStorage(failures=_REPORTED_PRUNE_FAILURES),
        clock=clock,
        stats=stats,
        on_error=warn,
    )

    sink.prune(dt.date(2026, 11, 6), 7)

    prune_stats = stats.snapshot()["prune"]
    assert prune_stats["failures"] == _REPORTED_PRUNE_FAILURES, (
        f"the wrapper must count each reported failure, saw {prune_stats!r}"
    )
    assert prune_stats["error_categories"] == ["OSError"]


def test_a_refused_location_reports_no_append_and_no_storage_failure(
    tmp_path: Path,
) -> None:
    """A location refusal is not a storage failure in the telemetry.

    The location question is answered before the append, so the collector sees
    an attempt with no failure category. Counting it as a storage failure would
    blame the filesystem for a deliberate refusal.
    """
    log_dir = tmp_path / "log"
    clock = _FixedClock(_WALL, 0.0)
    stats = log_mod.StorageStats()
    appender = _RecordingAppender(clock)
    sink = _sink(
        log_dir,
        clock=clock,
        stats=stats,
        appender=appender,
        policy=log_mod.WritePolicy(
            on_error=lambda _message: None,
            is_tracked=lambda _candidate: False,
            is_visible=lambda _candidate: True,
        ),
    )

    sink.write({"record_type": "start"})

    assert appender.calls == [], (
        "a refused location must never reach the adapter, but it saw"
        f" {appender.calls!r}"
    )
    write_stats = stats.snapshot()["write"]
    assert write_stats["attempts"] == 1
    assert write_stats["failures"] == 0, (
        f"a refusal is not a storage failure, but got {write_stats!r}"
    )


def test_the_log_writes_telemetry_from_its_own_collector() -> None:
    """A sink without telemetry does not deprive the log of its storage record."""
    sink = _RecordingSink()
    log = log_mod.DecisionLog(
        log_mod.InvocationContext(
            correlation={}, start_dir=Path("/repo"), base_ref="origin/main"
        ),
        storage=_SentinelStats(),
        sink=sink,
        # The process-facing collaborators a composition root binds are fixed
        # here so the test never reads the process clock, identifiers, or
        # signal table.
        clock=_FixedClock(_WALL, 0.0),
        run_id_factory=lambda: "run-1",
        pid=1,
        signal_name=lambda signum: f"SIG{signum}",
        terminal_commit=contextlib.nullcontext,
        on_error=warn,
    )

    log.finish()

    storage = [record for record in sink.records if record.get("event") == "storage"]
    assert len(storage) == 1, (
        "the log must write one storage record from the collector it was given,"
        f" but wrote {storage!r}"
    )
    assert storage[0]["operations"] == [_SentinelStats._SNAPSHOT["write"]], (
        "the storage record must carry the substituted collector's report,"
        f" but carried {storage[0]['operations']!r}"
    )
    assert any(record["record_type"] == "finish" for record in sink.records), (
        "the run must still finish through the substituted sink"
    )


#: Warnings a sink reported, collected so a test can assert on them.
warnings: list[str] = []


def _recorded_warnings(message: str) -> None:
    """Keep a bounded warning the sink reports instead of writing a stream."""
    warnings.append(message)
