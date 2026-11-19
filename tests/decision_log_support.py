"""Shared fixtures and helpers for the decision-log test modules.

Several behaviour modules need the same builders, fake clocks and in-memory
sinks. They live here rather than in one test module so no module has to import
another test module, and so each module stays within the repository's size
limit.
"""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

from post_turn_quality_stop_hook import decision_log as log_mod
from post_turn_quality_stop_hook.decision_log import (
    DecisionLog,
    DecisionLogSettings,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

DEFAULT_RETENTION = 14
CONFIG_RETENTION = 3
ENV_RETENTION = 2
FALLBACK_RETENTION = 9
# Owner-only access for the daily log files.
LOG_FILE_MODE = 0o600
# The injected clock starts 0.5 s after the supplied ``started`` timestamp.
EXPECTED_DURATION_MS = 500
# Each monotonic reading costs this much on the ticking test clock.
TICK_MS = 250
# The contended-lock test lets the clock advance twice by the retry interval.
CONTENDED_ATTEMPTS = 2
CONTENDED_WAIT_MS = 20

CORRELATION: dict[str, object] = {
    "session_id": "session-1",
    "transcript_path": "/logs/transcript.jsonl",
    "hook_event_name": "Stop",
    "stop_hook_active": False,
}


def _settings(
    tmp_path: Path, *, enabled: bool = True, retention_days: int = 14
) -> DecisionLogSettings:
    """Return settings pointing at a temporary state home."""
    return DecisionLogSettings(
        enabled=enabled,
        state_home=tmp_path / "state",
        retention_days=retention_days,
    )


def _log_files(state_home: Path) -> list[Path]:
    """Return decision-log files beneath a state home, oldest first."""
    return sorted(log_mod.log_directory(state_home).glob("hook-*.jsonl"))


def _read_records(state_home: Path) -> list[dict[str, object]]:
    """Parse every JSONL record written beneath a state home.

    Lines are read the way the hook's own reader reads them: one newline-
    terminated record at a time. ``str.splitlines`` would also split on
    characters such as U+2028, which JSON permits inside a string, so a record
    carrying one would be torn into fragments that no longer parse.
    """
    records: list[dict[str, object]] = []
    for path in _log_files(state_home):
        with path.open(encoding="utf-8") as handle:
            records.extend(
                typ.cast("dict[str, object]", json.loads(line)) for line in handle
            )
    return records


def _storage_record(records: list[dict[str, object]]) -> dict[str, object]:
    """Return the single storage telemetry record from a run."""
    storage = [record for record in records if record.get("event") == "storage"]
    assert len(storage) == 1, (  # ruff:ignore[assert]  # a helper must fail the calling test loudly.
        "expected exactly one storage record"
    )
    return storage[0]


def _make_log(settings: DecisionLogSettings) -> DecisionLog:
    """Construct a decision log with standard correlation fields."""
    return DecisionLog(
        settings,
        log_mod.InvocationContext(
            correlation=CORRELATION,
            start_dir=Path("/repo"),
            base_ref="origin/main",
        ),
    )


def _finish_record(records: list[dict[str, object]]) -> dict[str, object]:
    """Return the single finish record from a run."""
    finishes = [record for record in records if record["record_type"] == "finish"]
    assert len(finishes) == 1, (  # ruff:ignore[assert]  # a helper must fail the calling test loudly.
        "expected exactly one finish record"
    )
    return finishes[0]


def _log_line(record_type: str = "start") -> str:
    """Return one JSONL line shaped like a record the hook writes."""
    return (
        json.dumps({
            "schema_version": log_mod.SCHEMA_VERSION,
            "producer": log_mod.PRODUCER,
            "record_type": record_type,
            "run_id": "run-1",
            "pid": 1,
            "timestamp": "2026-11-06T00:00:00+00:00",
        })
        + "\n"
    )


def _hook_line_of_size(size: int) -> str:
    """Return one hook-shaped record encoded to exactly ``size`` bytes.

    The ownership reader rejects only a line *longer* than its limit, so tests
    can place a genuine file exactly on the boundary without writing a
    megabyte of literal JSON. The padding is ordinary JSON, which the reader
    ignores once the record's required fields verify.
    """
    record = json.loads(_log_line())
    record["pad"] = ""
    room = size - len(json.dumps(record))
    if room < 0:
        message = f"a hook record needs at least {-room} more bytes"
        raise ValueError(message)
    record["pad"] = "x" * room
    return json.dumps(record) + "\n"


def _resolver_failing_for(directory: Path) -> cabc.Callable[..., Path]:
    """Return a resolver that raises for ``directory`` and defers otherwise."""
    original = Path.resolve

    def _resolve(path: Path, *, strict: bool = False) -> Path:
        """Resolve every path except the failing directory."""
        if path == directory:
            raise OSError
        return original(path, strict=strict)

    return _resolve


class _FixedClock:
    """Clock returning fixed wall and monotonic values."""

    def __init__(self, wall: dt.datetime, monotonic: float) -> None:
        """Store the fixed values returned by both clock readings."""
        self._wall = wall
        self._monotonic = monotonic
        self.slept: list[float] = []

    def wall(self) -> dt.datetime:
        """Return the fixed wall-clock reading."""
        return self._wall

    def monotonic(self) -> float:
        """Return the fixed monotonic reading."""
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        """Record the requested wait without actually sleeping."""
        self.slept.append(seconds)


class _AdvancingClock:
    """Clock whose monotonic reading moves with each requested wait.

    A fixed reading would make the sink's retry loop run forever, because the
    deadline could never be observed as reached. Advancing on ``sleep`` models
    the real clock closely enough for the lock timeout to be exercised without
    any wall-clock delay.
    """

    def __init__(self, wall: dt.datetime) -> None:
        """Start ``wall`` at ``wall`` and the monotonic reading at zero."""
        self._wall = wall
        self._monotonic = 0.0
        self.slept: list[float] = []

    def wall(self) -> dt.datetime:
        """Return the fixed wall-clock reading."""
        return self._wall

    def monotonic(self) -> float:
        """Return the accumulated monotonic reading."""
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        """Advance the monotonic reading by the requested wait."""
        self.slept.append(seconds)
        self._monotonic += seconds


class _TickingClock(_AdvancingClock):
    """Clock that charges a fixed cost for every monotonic reading."""

    def __init__(self, wall: dt.datetime, step: float) -> None:
        """Start at ``wall`` with a reading that grows by ``step``."""
        super().__init__(wall)
        self._step = step

    def monotonic(self) -> float:
        """Return a reading and advance the clock by one step."""
        self._monotonic += self._step
        return self._monotonic


class _RollingClock:
    """Clock whose wall reading can be advanced past midnight."""

    def __init__(self, wall: dt.datetime) -> None:
        """Start at ``wall`` with a monotonic reading of zero."""
        self._wall = wall
        self._monotonic = 0.0

    def advance(self, delta: dt.timedelta) -> None:
        """Move the wall clock forward by ``delta``."""
        self._wall += delta

    def wall(self) -> dt.datetime:
        """Return the current wall-clock reading."""
        return self._wall

    def monotonic(self) -> float:
        """Return a monotonic reading that never moves backwards."""
        self._monotonic += 1.0
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        """Advance the monotonic reading by the requested wait."""
        self._monotonic += seconds


class _RecordingSink:
    """Sink that keeps records in memory."""

    def __init__(self) -> None:
        """Start with empty record and prune logs."""
        self.records: list[dict[str, object]] = []
        self.pruned: list[tuple[dt.date, int]] = []

    def write(self, record: typ.Mapping[str, object]) -> None:
        """Keep a copy of the written record."""
        self.records.append(dict(record))

    def prune(self, today: dt.date, retention_days: int) -> None:
        """Record the date and window passed to a retention prune."""
        self.pruned.append((today, retention_days))


class _RaisingSink:
    """Sink that fails every write, as an injected adapter might."""

    def write(self, record: typ.Mapping[str, object]) -> None:  # ruff:ignore[no-self-use]  # instance method is the contract.
        """Fail every write."""
        raise RuntimeError

    def prune(self, today: dt.date, retention_days: int) -> None:
        """Succeed without pruning, so only the write fails."""
