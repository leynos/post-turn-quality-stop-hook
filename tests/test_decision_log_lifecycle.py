"""The run's record sequence, checked against a state machine."""

from __future__ import annotations

import datetime as dt
import signal
import typing as typ
from pathlib import Path

import pytest
from decision_log_support import (
    CORRELATION,
    EXPECTED_DURATION_MS,
    _compose_log,
    _finish_record,
    _FixedClock,
    _RaisingSink,
    _read_records,
    _RecordingSink,
    _settings,
    _storage_record,
)
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

from post_turn_quality_stop_hook import decision_log as log_mod
from post_turn_quality_stop_hook.decision_log import (
    DecisionLogSettings,
    InvocationContext,
)
from post_turn_quality_stop_hook.diagnostics import warn

_SHORT_TEXT = st.text(alphabet=st.characters(whitelist_categories=("Ll",)), max_size=12)
# ``SIGHUP`` and ``SIGTERM`` are POSIX names. Reading one directly in the class
# body would fail at import time on Windows and take the whole session down
# with a collection error, so the strategy draws only from signals this
# platform defines.
_SIGNALS = tuple(
    getattr(signal, name)
    for name in ("SIGHUP", "SIGINT", "SIGTERM")
    if hasattr(signal, name)
)


class _LogLifecycleMachine(RuleBasedStateMachine):
    """Drive a decision log through arbitrary orderings of its mutators."""

    def __init__(self) -> None:
        """Start a log whose sink, clock and identifier are observed."""
        super().__init__()
        self.sink = _RecordingSink()
        self.log = _compose_log(
            DecisionLogSettings(state_home=Path("/unused")),
            InvocationContext(
                correlation=CORRELATION,
                start_dir=Path("/repo"),
                base_ref="origin/main",
            ),
            sink=self.sink,
            clock=_FixedClock(dt.datetime(2026, 11, 6, tzinfo=dt.UTC), 0.0),
            run_id_factory=lambda: "run-1",
            on_error=warn,
        )
        self.gates: dict[str, tuple[str, str | None]] = {}
        self.blocked = False
        self.block_reason: str | None = None
        self.disabled = False
        self.finished = False
        self.finish_written = False
        self.expected_finish: dict[str, object] | None = None

    def _finish(
        self,
        decision: str | None,
        exit_code: int,
        error: str | None,
        signal_name: str | None = None,
    ) -> None:
        """Apply one finish call to the model, mirroring the log's guards."""
        if self.finished:
            return
        self.finished = True
        if self.disabled:
            return
        self.finish_written = True
        # Every recorded gate carries a positional ``error`` field, set only
        # when the gate failed open on a Git error; the model mirrors it.
        gates = [
            {
                "gate": gate,
                "outcome": self.gates[gate][0],
                "reason": self.gates[gate][1],
                "error": None,
            }
            if gate in self.gates
            else {"gate": gate, "outcome": "not_reached", "reason": None}
            for gate in log_mod.GATE_ORDER
        ]
        expected: dict[str, object] = {
            "decision": decision or ("block" if self.blocked else "allow"),
            "exit_code": exit_code,
            "error": error,
            "gates": gates,
        }
        if self.block_reason is not None:
            expected["block_reason"] = self.block_reason
        if signal_name is not None:
            expected["signal"] = signal_name
        self.expected_finish = expected

    @rule(
        gate=st.sampled_from(log_mod.GATE_ORDER),
        outcome=st.sampled_from(("pass", "skip", "block")),
        reason=st.one_of(st.none(), _SHORT_TEXT),
    )
    def record_gate(self, gate: str, outcome: str, reason: str | None) -> None:
        """Record a gate outcome, replacing any earlier outcome for it."""
        self.log.record_gate(gate, outcome, reason)
        self.gates[gate] = (outcome, reason)

    @rule(reason=st.one_of(st.none(), _SHORT_TEXT))
    def mark_block(self, reason: str | None) -> None:
        """Mark the run blocked, keeping the latest reason."""
        self.log.mark_block(reason)
        self.blocked = True
        self.block_reason = reason

    @rule(
        decision=st.sampled_from((None, "allow", "block", "error")),
        exit_code=st.integers(min_value=0, max_value=255),
        error=st.one_of(st.none(), _SHORT_TEXT),
    )
    def finish(self, decision: str | None, exit_code: int, error: str | None) -> None:
        """Finish the run, including repeated calls after it has finished."""
        self._finish(decision, exit_code, error)
        # Call unconditionally so the log's own guards are exercised: a repeat
        # finish, or one after ``disable``, must write nothing.
        self.log.finish(decision=decision, exit_code=exit_code, error=error)

    @rule(signum=st.sampled_from(_SIGNALS))
    def record_signal(self, signum: int) -> None:
        """Record an interrupted run before the process exits."""
        self._finish(
            "interrupted",
            128 + signum,
            None,
            signal_name=signal.Signals(signum).name,
        )
        # As with ``finish``, call unconditionally so a signal after a finish
        # or after ``disable`` is proven not to write a second record.
        self.log.record_signal(signum)

    @rule()
    def disable(self) -> None:
        """Opt out after the start record, writing a terminal ``disabled`` finish.

        The opt-out is observable: the finish record says ``disabled``, so a
        missing finish still means the run was interrupted. Any later finish
        or signal record is suppressed by the log's own guards.
        """
        self._finish("disabled", 0, None)
        self.log.disable()
        self.disabled = True

    @invariant()
    def records_match_the_model(self) -> None:
        """Assert one start, at most one finish, and nothing else in the sink."""
        records = self.sink.records
        kinds = [record["record_type"] for record in records]
        assert kinds.count("start") == 1, f"expected one start but got {kinds!r}"
        assert records[0]["record_type"] == "start"
        assert {record["run_id"] for record in records} == {"run-1"}
        finishes = [record for record in records if record["record_type"] == "finish"]
        assert len(finishes) == (1 if self.finish_written else 0), (
            f"expected finish_written={self.finish_written!r} but got {finishes!r}"
        )
        if not self.finish_written:
            return
        finish = finishes[0]
        expected = self.expected_finish
        assert expected is not None, "a written finish needs an expectation"
        for key, value in expected.items():
            assert finish[key] == value, (
                f"expected {key}={value!r} but got {finish[key]!r}"
            )
        gates = typ.cast("list[dict[str, object]]", finish["gates"])
        assert [gate["gate"] for gate in gates] == list(log_mod.GATE_ORDER)


def test_any_log_lifecycle_matches_its_model() -> None:
    """Arbitrary orderings of gates, blocks, signals and finishes stay coherent."""
    run_state_machine_as_test(
        _LogLifecycleMachine,
        settings=settings(max_examples=50, deadline=None),
    )


class TestInjectedDependencies:
    """Record collection depends on narrow, substitutable interfaces."""

    def test_injected_clock_and_run_id_shape_the_records(self, tmp_path: Path) -> None:
        """The collector uses the supplied clock and identifier source."""
        wall = dt.datetime(2026, 11, 6, 12, 0, tzinfo=dt.UTC)
        log = _compose_log(
            _settings(tmp_path),
            InvocationContext(
                correlation={"session_id": "session-1"},
                start_dir=Path("/work"),
                base_ref="origin/main",
            ),
            clock=_FixedClock(wall, 100.0),
            run_id_factory=lambda: "run-fixed",
            started=99.5,
            on_error=warn,
        )

        log.finish()

        records = _read_records(tmp_path / "state")
        assert records[0]["run_id"] == "run-fixed"
        assert records[0]["timestamp"] == "2026-11-06T12:00:00+00:00"
        assert records[-1]["duration_ms"] == EXPECTED_DURATION_MS

    def test_injected_sink_receives_every_record(self, tmp_path: Path) -> None:
        """Persistence is substitutable without a filesystem."""
        sink = _RecordingSink()
        log = _compose_log(
            _settings(tmp_path),
            InvocationContext(
                correlation={}, start_dir=Path("/work"), base_ref="origin/main"
            ),
            sink=sink,
            clock=_FixedClock(dt.datetime(2026, 11, 6, tzinfo=dt.UTC), 10.0),
            on_error=warn,
        )

        assert sink.pruned == [], (
            "construction must not prune before the window is resolved"
        )
        log.retention.apply(9)
        with log.stage("prepare"):
            pass
        log.finish()

        assert [record["record_type"] for record in sink.records] == [
            "start",
            "progress",
            "progress",
            "finish",
        ]
        assert sink.pruned == [(dt.date(2026, 11, 6), 9)]

    def test_sink_failure_does_not_escape(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A raising sink warns without replacing the decision."""
        log = _compose_log(
            _settings(tmp_path),
            InvocationContext(
                correlation={}, start_dir=Path("/work"), base_ref="origin/main"
            ),
            sink=_RaisingSink(),
            on_error=warn,
        )

        with log.stage("prepare"):
            pass
        log.finish(decision="allow", exit_code=0)

        assert "could not record a decision-log entry" in capsys.readouterr().err

    def test_disable_writes_a_terminal_disabled_finish(self, tmp_path: Path) -> None:
        """An opt-out is visible as a finish, not as a missing one.

        A missing finish means the run was interrupted. Writing a terminal
        record with ``decision = "disabled"`` keeps that meaning while making
        the opt-out joinable to the run, and every later record is suppressed.
        """
        log = _compose_log(
            _settings(tmp_path),
            InvocationContext(
                correlation={}, start_dir=Path("/work"), base_ref="origin/main"
            ),
            clock=_FixedClock(dt.datetime(2026, 11, 6, tzinfo=dt.UTC), 0.0),
            on_error=warn,
        )

        log.disable()
        before = len(_read_records(tmp_path / "state"))
        # The opt-out must suppress every later progress record, not only the
        # terminal ones: correlation and stages are the records a caller could
        # otherwise keep emitting for the rest of the run.
        log.set_correlation({"session_id": "session-1"})
        with log.stage("prepare"):
            pass
        log.finish(decision="allow")
        log.record_signal(signal.SIGTERM)

        records = _read_records(tmp_path / "state")
        assert len(records) == before, "an opt-out suppresses all later records"
        assert [record["record_type"] for record in records] == ["start", "finish"]
        assert records[-1]["decision"] == "disabled"
        assert records[-1]["exit_code"] == 0

    def test_finish_reports_bounded_storage_statistics(self, tmp_path: Path) -> None:
        """Storage telemetry is its own record, without paths or error text."""
        log = _compose_log(
            _settings(tmp_path),
            InvocationContext(
                correlation={}, start_dir=Path("/work"), base_ref="origin/main"
            ),
            clock=_FixedClock(dt.datetime(2026, 11, 6, tzinfo=dt.UTC), 0.0),
            on_error=warn,
        )

        log.retention.apply(7)
        log.finish()

        records = _read_records(tmp_path / "state")
        storage = _storage_record(records)
        operations = typ.cast("list[dict[str, object]]", storage["operations"])
        write, prune = operations
        assert write["operation"] == "write"
        # The statistics are captured before the finish record itself is
        # written, so they describe the start record alone here.
        assert write["attempts"] == 1
        assert write["failures"] == 0
        assert isinstance(write["storage_ms"], int)
        assert isinstance(write["lock_wait_ms"], int)
        assert write["error_categories"] == []
        assert prune["operation"] == "prune"
        assert prune["attempts"] == 1
        assert prune["failures"] == 0
        assert prune["error_categories"] == []
        assert _finish_record(records)["record_type"] == "finish"
