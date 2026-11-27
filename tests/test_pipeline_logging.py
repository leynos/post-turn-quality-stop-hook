"""Exercise the decision log's pipeline seam on the slimmed orchestrator.

The branch this file belongs to carries only the durable decision log. These
tests assert that the log records what the pipeline decided: gate outcomes,
quality selection, the commands a stage ran, repository facts, and the block
marker. They drive the real ``run_stop_checks`` boundary with a prepared
observer so the assertions describe the log's own behaviour rather than a
particular set of adapters.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import typing as typ
from pathlib import Path
from unittest import mock

import pytest
from branch_support import (
    FakeBranchState,
    gate_ports,
    pull_request,
)
from decision_log_support import _make_log, _settings
from observer_support import RecordingObserver as _RecordingObserver
from observer_support import no_warnings as _no_warnings
from quality_support import UnusedRepositoryFacts, enumerated
from quality_support import command as _command

from post_turn_quality_stop_hook import decision_log as log_mod
from post_turn_quality_stop_hook import observation as observation_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import process as process_mod
from post_turn_quality_stop_hook import quality as quality_mod
from post_turn_quality_stop_hook import repository_facts as facts_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook import upstream_port as upstream_port_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.git_facts import GitFacts
from post_turn_quality_stop_hook.observer import (
    NullObserver,
    PipelineObserver,
    observer_for,
)
from post_turn_quality_stop_hook.recording import InvocationLog

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook.driver import BuildDriver


REPO = Path("/fake/repo")

#: Publishes one evaluation makes: the pre-enumeration selection, each started
#: group, and the final selection.
_QUALITY_SELECTION_PUBLISHES = 3


def _decision_records(state_home: Path) -> list[dict[str, object]]:
    """Read every record from the test's decision-log files."""
    log_dir = log_mod.log_directory(state_home)
    records: list[dict[str, object]] = []
    for path in sorted(log_dir.glob("hook-*.jsonl")):
        records.extend(
            typ.cast("dict[str, object]", json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    return records


def _finish_record(state_home: Path) -> dict[str, object]:
    """Return the run's single finish record."""
    return next(
        record
        for record in _decision_records(state_home)
        if record["record_type"] == "finish"
    )


def _gate_outcomes(finish: dict[str, object]) -> dict[str, str]:
    """Return each gate's recorded outcome from a finish record."""
    return {
        typ.cast("str", gate["gate"]): typ.cast("str", gate["outcome"])
        for gate in typ.cast("list[dict[str, object]]", finish["gates"])
    }


def _observer(tmp_path: Path) -> tuple[log_mod.DecisionLog, PipelineObserver]:
    """Build a decision log and the observer that writes to it."""
    log = _make_log(_settings(tmp_path))
    return log, observer_for(InvocationLog(recorder=log), warn=_no_warnings)


_no_facts_source = UnusedRepositoryFacts()


def _no_upstream_source(
    _repo: Path, _facts: GitFacts | None, _observer: object
) -> upstream_port_mod.UpstreamHeadSource:
    """Build an upstream port for a run whose gate is disabled."""

    class _Unused:
        """Fail if the pipeline asks this port for a comparison."""

        def refresh_comparison(self) -> upstream_port_mod.UpstreamComparison | None:
            """Fail: a disabled gate must not consult the port."""
            pytest.fail("the upstream port must not be consulted")

    return _Unused()


def _events_of(observer: _RecordingObserver, name: str) -> list[object]:
    """Return the payloads of every event of one kind."""
    return [payload for kind, payload in observer.events if kind == name]


def _telemetry_events(observer: _RecordingObserver) -> list[str]:
    """Return the event names of every telemetry record, in order."""
    telemetry = typ.cast(
        "list[tuple[str, dict[str, object]]]",
        _events_of(observer, "telemetry"),
    )
    return [event for event, _fields in telemetry]


# ---------------------------------------------------------------------------
# Gate decisions
# ---------------------------------------------------------------------------


class TestGateRecording:
    """The branch-state gates record one outcome each, through the observer."""

    def test_each_gate_is_recorded_with_its_reason(self, tmp_path: Path) -> None:
        """Every gate's outcome and reason reach the finish record."""
        log, observer = _observer(tmp_path)
        state = state_mod.HookState()
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            config=Config(gate_unpushed_commits=False, gate_pr_rebase=False),
        )
        with (
            mock.patch.object(
                pipeline_mod,
                "uncommitted_changes_gate",
                return_value=pipeline_mod.BranchStateGateDecision(
                    gate="uncommitted_changes", outcome="pass", reason="clean"
                ),
            ),
            mock.patch.object(
                pipeline_mod,
                "pr_rebase_check",
                return_value=0,
            ),
        ):
            rc = pipeline_mod.run_branch_state_gates(
                REPO,
                state,
                options,
                _no_upstream_source(REPO, state.git_facts, observer),
                observer=observer,
                ports=gate_ports(),
            )
        log.finish()

        assert rc == 0
        finish = _finish_record(tmp_path / "state")
        outcomes = _gate_outcomes(finish)
        assert outcomes["uncommitted_changes"] == "pass"
        assert outcomes["unpushed_commits"] == "skip"

    def test_gate_stages_are_recorded(self, tmp_path: Path) -> None:
        """Each gate runs in its own stage so a timeout is locatable."""
        log, observer = _observer(tmp_path)
        state = state_mod.HookState()
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            config=Config(gate_unpushed_commits=False, gate_pr_rebase=False),
        )
        with (
            mock.patch.object(
                pipeline_mod,
                "uncommitted_changes_gate",
                return_value=pipeline_mod.BranchStateGateDecision(
                    gate="uncommitted_changes", outcome="pass", reason="clean"
                ),
            ),
            mock.patch.object(pipeline_mod, "pr_rebase_check", return_value=0),
        ):
            pipeline_mod.run_branch_state_gates(
                REPO,
                state,
                options,
                _no_upstream_source(REPO, state.git_facts, observer),
                observer=observer,
                ports=gate_ports(),
            )
        log.finish()

        stages = typ.cast(
            "dict[str, int]", _finish_record(tmp_path / "state")["stages"]
        )
        assert {
            "gate_uncommitted_changes",
            "gate_unpushed_commits",
            "gate_pr_rebase",
        } <= set(stages)

    def test_a_blocking_gate_marks_the_run(self, tmp_path: Path) -> None:
        """A blocking gate records its block marker with a fixed reason."""
        log, observer = _observer(tmp_path)
        state = state_mod.HookState()
        options = state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
        with mock.patch.object(
            pipeline_mod,
            "uncommitted_changes_gate",
            return_value=pipeline_mod.BranchStateGateDecision(
                gate="uncommitted_changes",
                outcome="block",
                payload=state_mod.BlockPayload(template="uncommitted_required.j2"),
                reason="uncommitted changes present",
            ),
        ):
            pipeline_mod.run_branch_state_gates(
                REPO,
                state,
                options,
                _no_upstream_source(REPO, state.git_facts, observer),
                observer=observer,
                ports=gate_ports(),
            )
        log.finish()

        finish = _finish_record(tmp_path / "state")
        assert finish["decision"] == "block"
        assert finish["block_reason"] == "uncommitted changes present"
        assert _gate_outcomes(finish)["uncommitted_changes"] == "block"

    def test_a_git_error_is_not_recorded_as_a_pass(self, tmp_path: Path) -> None:
        """A failed query carries its error so a pass is never assumed."""
        log, observer = _observer(tmp_path)
        state = state_mod.HookState()
        options = state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
        error = HookError("has_uncommitted_changes", "git status failed")
        with mock.patch.object(
            pipeline_mod,
            "uncommitted_changes_gate",
            return_value=pipeline_mod.BranchStateGateDecision(
                gate="uncommitted_changes",
                outcome="pass",
                error=error,
                reason="git error",
            ),
        ):
            pipeline_mod.run_branch_state_gates(
                REPO,
                state,
                options,
                _no_upstream_source(REPO, state.git_facts, observer),
                observer=observer,
                ports=gate_ports(),
            )
        log.finish()

        gates = typ.cast(
            "list[dict[str, object]]", _finish_record(tmp_path / "state")["gates"]
        )
        entry = next(gate for gate in gates if gate["gate"] == "uncommitted_changes")
        assert entry["outcome"] == "pass"
        assert entry["error"] == log_mod.GATE_ERROR_CATEGORY, (
            "a failed query is recorded as a closed category, never as its text"
        )

    def test_a_failed_query_without_a_warning_emits_a_fixed_warning(self) -> None:
        """A gate that could not answer still tells the operator it failed open.

        With logging disabled the decision's error is discarded, so the
        boundary emits a fixed, gate-specific warning instead of continuing
        silently. The Git diagnostic itself never becomes a warning.
        """
        observer = _RecordingObserver()
        decision = state_mod.BranchStateGateDecision(
            gate="uncommitted_changes",
            outcome="pass",
            error=HookError("has_uncommitted_changes", "git diff failed: secret"),
            reason="git error",
        )

        observation_mod.record_gate_decision(observer, decision)

        assert _events_of(observer, "warning") == [
            (
                "uncommitted_changes gate could not verify its repository state;"
                " continuing without blocking"
            )
        ], "a failed query must produce the fixed gate warning"

    def test_an_existing_gate_warning_is_preserved(self) -> None:
        """A warning the gate composed is the only one emitted."""
        observer = _RecordingObserver()
        decision = state_mod.BranchStateGateDecision(
            gate="unpushed_commits",
            outcome="pass",
            error=HookError("unpushed", "git rev-list failed"),
            reason="git error",
            warning="unpushed gate could not verify the upstream",
        )

        observation_mod.record_gate_decision(observer, decision)

        assert _events_of(observer, "warning") == [
            "unpushed gate could not verify the upstream"
        ]


class TestPrRebaseRecording:
    """The real rebase gate reports through the observer the run supplies.

    These tests deliberately leave ``pr_rebase_check`` and ``_pr_rebase_outcome``
    unpatched: the point is that ``run_branch_state_gates`` passes the run's
    observer on, so the gate's outcome and reason reach the finish record.
    Only the Git and GitHub adapters the gate queries are replaced, so no
    network or repository is consulted.
    """

    @pytest.mark.parametrize(
        ("base_oid", "ancestor", "outcome", "reason", "expected_decision"),
        [
            (
                "local",
                None,
                "pass",
                "pull request base is not ahead",
                "allow",
            ),
            (
                "remote",
                True,
                "block",
                "pull request base is ahead",
                "block",
            ),
        ],
        ids=["allow", "block"],
    )
    def test_the_pr_rebase_outcome_reaches_the_finish_record(  # ruff:ignore[too-many-arguments,too-many-positional-arguments]  # pytest injects the parametrized case fields positionally.
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        base_oid: str,
        ancestor: bool | None,
        outcome: typ.Literal["pass", "block"],
        reason: str,
        expected_decision: str,
    ) -> None:
        """Both the pass and the block path record the rebase gate.

        ``base_oid`` is the comparison's view of the remote base commit: when
        it equals the computed merge base the gate passes without asking about
        ancestry, and a different value with an ancestor answer blocks. The
        gate function, its decision logic, the decision recording, and the
        block marker all run for real through ``run_branch_state_gates``.
        """
        log, observer = _observer(tmp_path)
        state = state_mod.HookState(
            git_facts=GitFacts(
                primary_remote="origin",
                upstream_ref="origin/feature",
                pr_base_local_ref=None,
                local_base_commit="base",
                three_way_merge_is_configured=False,
            )
        )
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            config=Config(gate_unpushed_commits=False),
        )
        ports = gate_ports(
            FakeBranchState(merged="local", ancestor=ancestor),
            summary=pull_request(base_oid),
            available=frozenset({"check-fmt", "lint", "typecheck"}),
        )
        with mock.patch.object(
            pipeline_mod,
            "uncommitted_changes_gate",
            return_value=pipeline_mod.BranchStateGateDecision(
                gate="uncommitted_changes", outcome="pass", reason="clean"
            ),
        ):
            rc = pipeline_mod.run_branch_state_gates(
                REPO,
                state,
                options,
                _no_upstream_source(REPO, state.git_facts, observer),
                observer=observer,
                ports=ports,
            )
        log.finish()

        assert rc == 0
        finish = _finish_record(tmp_path / "state")
        gates = typ.cast("list[dict[str, object]]", finish["gates"])
        entry = next(gate for gate in gates if gate["gate"] == "pr_rebase")
        assert entry["outcome"] == outcome, (
            "the rebase outcome must reach the finish record"
        )
        assert entry["reason"] == reason, (
            "the rebase reason must reach the finish record"
        )
        assert finish["decision"] == expected_decision
        capsys.readouterr()


# ---------------------------------------------------------------------------
# Quality decisions and their telemetry
# ---------------------------------------------------------------------------


class TestQualityRecording:
    """The quality gate records under quality-specific event names."""

    def test_a_quality_skip_is_recorded_under_its_own_event(self) -> None:
        """A quality skip is named ``quality_gate_skip``, not a branch event."""
        observer = _RecordingObserver()
        state = state_mod.HookState(changed_files=("src/app.py",))
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            config=Config(gate_quality_checks=False),
        )

        result = pipeline_mod.run_quality_checks_if_needed(
            state, REPO, options, observer=observer
        )

        assert result is None
        events = _telemetry_events(observer)
        assert events == ["quality_gate_skip"], (
            f"expected only the quality event but got {events!r}"
        )
        assert not any(
            str(event).startswith("branch_state_gate_") for event in events
        ), "a quality skip must not be reported as a branch-state gate"
        decisions = _events_of(observer, "gate_decided")
        assert decisions == [
            ("quality_checks", "skip", "gate disabled by configuration", None)
        ]

    def test_a_quality_block_is_recorded_and_marked(self) -> None:
        """A failing quality target records a block and marks the run."""
        observer = _RecordingObserver()
        state = state_mod.HookState(changed_files=("src/app.py",))
        options = state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
        driver = _driver()
        failing = _command(exit_code=1)
        with (
            mock.patch.object(pipeline_mod, "select_build_driver", return_value=driver),
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value=enumerated({"check-fmt", "lint", "typecheck"}),
            ),
            mock.patch.object(pipeline_mod, "run_build_targets", return_value=failing),
        ):
            result = pipeline_mod.run_quality_checks_if_needed(
                state, REPO, options, observer=observer
            )

        assert result is not None
        assert result.ok is False
        events = _telemetry_events(observer)
        assert events == ["quality_gate_block"], (
            f"expected the quality block event but got {events!r}"
        )
        assert _events_of(observer, "blocked") == ["quality checks failed"]

    def test_a_quality_pass_is_recorded_without_telemetry(self) -> None:
        """A pass is recorded as a gate decision but needs no telemetry."""
        observer = _RecordingObserver()
        state = state_mod.HookState(changed_files=("src/app.py",))
        options = state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
        with (
            mock.patch.object(
                pipeline_mod, "select_build_driver", return_value=_driver()
            ),
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value=enumerated({"check-fmt", "lint", "typecheck"}),
            ),
            mock.patch.object(
                pipeline_mod, "run_build_targets", return_value=_command()
            ),
        ):
            result = pipeline_mod.run_quality_checks_if_needed(
                state, REPO, options, observer=observer
            )

        assert result is None
        assert _events_of(observer, "telemetry") == []
        assert _events_of(observer, "gate_decided") == [
            ("quality_checks", "pass", "quality checks passed", None)
        ]

    def test_quality_selection_facts_reach_the_observer(self) -> None:
        """The selection the stage made is published as bounded fields."""
        observer = _RecordingObserver()
        driver = _driver()
        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=driver,
            max_out=12000,
            enumerate_targets=lambda _repo, _driver: enumerated({"check-fmt", "lint"}),
            run_commands=lambda _repo, _request, _max_out: _command(),
            observer=observer,
        )

        quality_mod.evaluate_changes(
            stage, quality_mod.select_categories(("src/app.py",))
        )

        selections = typ.cast(
            "list[dict[str, object]]", _events_of(observer, "quality_selected")
        )
        assert len(selections) == _QUALITY_SELECTION_PUBLISHES, (
            "the driver and request list are published before enumeration, the"
            " completed group is published before the next starts, and the final"
            f" selection follows, got {selections!r}"
        )
        early = selections[0]
        assert early["build_driver"] == "make"
        assert early["build_driver_reason"] == "selected by configuration", (
            "the fixed selection reason must reach the record"
        )
        assert early["targets_requested"] == ["check-fmt", "lint", "typecheck"]
        assert early["targets_available"] == [], (
            "nothing is available before enumeration has run"
        )
        fields = selections[-1]
        assert fields["build_driver"] == "make"
        assert fields["build_driver_reason"] == "selected by configuration"
        assert fields["targets_requested"] == ["check-fmt", "lint", "typecheck"]
        assert fields["targets_available"] == ["check-fmt", "lint"]
        assert fields["targets_skipped"] == ["typecheck"]

    def test_each_command_is_recorded_with_its_own_status(self) -> None:
        """A started command gets a finish record carrying its exit status."""
        observer = _RecordingObserver()
        driver = _driver()
        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=driver,
            max_out=12000,
            enumerate_targets=lambda _repo, _driver: enumerated({"check-fmt", "lint"}),
            run_commands=lambda _repo, _request, _max_out: _command(exit_code=1),
            observer=observer,
        )

        quality_mod.evaluate_changes(
            stage, quality_mod.select_categories(("src/app.py",))
        )

        starts = _events_of(observer, "command_started")
        finishes = _events_of(observer, "command_finished")
        assert starts == [("code", "make check-fmt lint", ("check-fmt", "lint"))]
        assert finishes == [("code", 1)]

    def test_make_enumeration_records_no_command(self) -> None:
        """Parsing a Makefile runs nothing, so it records no command."""
        observer = _RecordingObserver()
        driver = _driver()
        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=driver,
            max_out=12000,
            enumerate_targets=lambda _repo, _driver: enumerated({"check-fmt"}),
            run_commands=lambda _repo, _request, _max_out: _command(),
            observer=observer,
        )

        quality_mod.evaluate_changes(
            stage, quality_mod.select_categories(("src/app.py",))
        )

        starts = typ.cast(
            "list[tuple[str, str, object]]", _events_of(observer, "command_started")
        )
        assert [start[0] for start in starts] == ["code"], (
            "only the build group is a command; the Makefile read is not"
        )

    def test_netsuke_enumeration_is_recorded(self) -> None:
        """Netsuke generation is a blocking command, so it is recorded."""
        observer = _RecordingObserver()
        driver = _netsuke_driver()
        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=driver,
            max_out=12000,
            enumerate_targets=lambda _repo, _driver: enumerated({"check-fmt"}),
            run_commands=lambda _repo, _request, _max_out: _command(),
            observer=observer,
        )

        quality_mod.evaluate_changes(
            stage, quality_mod.select_categories(("src/app.py",))
        )

        starts = [
            typ.cast("tuple[str, str, object]", start)
            for start in _events_of(observer, "command_started")
        ]
        assert starts[0][:2] == ("targets", "netsuke generate"), (
            f"expected a recorded generation command but got {starts!r}"
        )

    def test_a_failing_netsuke_generation_records_its_real_exit_status(self) -> None:
        """The generation record carries the child's status, not a synthetic one.

        ``netsuke generate`` can fail for its own reasons, and the record is
        what an operator reads afterwards. Reporting a fixed ``1`` would
        describe neither a child that exited ``3`` nor a launch that never
        produced a status at all.
        """
        observer = _RecordingObserver()
        driver = _netsuke_driver()
        failure = HookError("get_netsuke_targets", "generate failed")
        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=driver,
            max_out=12000,
            enumerate_targets=lambda _repo, _driver: enumerated(failure, exit_status=3),
            run_commands=lambda _repo, _request, _max_out: pytest.fail(
                "no build group may run after an enumeration failure"
            ),
            observer=observer,
        )

        quality_mod.evaluate_changes(
            stage, quality_mod.select_categories(("src/app.py",))
        )

        assert _events_of(observer, "command_finished") == [("targets", 3)], (
            "the recorded status must be the one netsuke generate returned"
        )

    def test_a_launch_failure_records_the_launch_failure_status(self) -> None:
        """A generation that never started has no child status to record."""
        observer = _RecordingObserver()
        driver = _netsuke_driver()
        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=driver,
            max_out=12000,
            enumerate_targets=lambda _repo, _driver: enumerated(
                HookError("get_netsuke_targets", "netsuke not found on PATH"),
                launched=False,
            ),
            run_commands=lambda _repo, _request, _max_out: pytest.fail(
                "no build group may run when netsuke cannot start"
            ),
            observer=observer,
        )

        quality_mod.evaluate_changes(
            stage, quality_mod.select_categories(("src/app.py",))
        )

        assert _events_of(observer, "command_finished") == [
            ("targets", process_mod.LAUNCH_FAILURE_STATUS)
        ], "a launch failure must be distinguishable from a status the child returned"

    def test_a_build_group_that_never_started_records_the_launch_failure(
        self,
    ) -> None:
        """A group whose executable never ran has no child status to record."""
        observer = _RecordingObserver()
        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=_driver(),
            max_out=12000,
            enumerate_targets=lambda _repo, _driver: enumerated({"check-fmt"}),
            run_commands=lambda _repo, _request, _max_out: _command(
                exit_code=127, launched=False
            ),
            observer=observer,
        )

        quality_mod.evaluate_changes(
            stage, quality_mod.select_categories(("src/app.py",))
        )

        assert _events_of(observer, "command_finished") == [
            ("code", process_mod.LAUNCH_FAILURE_STATUS)
        ], "a group that never started must not be recorded as a child status"

    def test_a_started_build_group_keeps_its_own_status(self) -> None:
        """A child that started and exited 127 keeps that status."""
        observer = _RecordingObserver()
        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=_driver(),
            max_out=12000,
            enumerate_targets=lambda _repo, _driver: enumerated({"check-fmt"}),
            run_commands=lambda _repo, _request, _max_out: _command(
                exit_code=127, launched=True
            ),
            observer=observer,
        )

        quality_mod.evaluate_changes(
            stage, quality_mod.select_categories(("src/app.py",))
        )

        assert _events_of(observer, "command_finished") == [("code", 127)], (
            "a launched child's own status must be preserved"
        )

    def _selected(
        self,
        observer: _RecordingObserver,
        *,
        select_driver: object,
        targets: set[str] | HookError | None = None,
    ) -> state_mod.HookState | None:
        """Run the quality gate with prepared driver and target answers."""
        state = state_mod.HookState(changed_files=("src/app.py",))
        options = state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
        targets_patch = (
            mock.patch.object(
                pipeline_mod, "get_build_targets", return_value=enumerated(targets)
            )
            if targets is not None
            else contextlib.nullcontext()
        )
        with (
            mock.patch.object(pipeline_mod, "select_build_driver", select_driver),
            targets_patch,
        ):
            return pipeline_mod.run_quality_checks_if_needed(
                state, REPO, options, observer=observer
            )

    def test_a_driver_selection_failure_records_the_requested_targets(self) -> None:
        """The request list survives a stop before any target is enumerated."""
        observer = _RecordingObserver()
        error = HookError("select_build_driver", "netsuke not found")

        result = self._selected(observer, select_driver=mock.Mock(return_value=error))

        assert result is not None
        selections = typ.cast(
            "list[dict[str, object]]", _events_of(observer, "quality_selected")
        )
        assert len(selections) == 1, (
            f"expected the request list to be published but got {selections!r}"
        )
        assert selections[0]["build_driver"] == "", (
            "no driver was selected, so the field must stay empty"
        )
        assert selections[0]["targets_requested"] == [
            "check-fmt",
            "lint",
            "typecheck",
        ]
        assert selections[0]["targets_available"] == []
        assert selections[0]["targets_skipped"] == []
        assert selections[0]["targets_run"] == []
        assert _events_of(observer, "gate_decided") == [
            ("quality_checks", "block", "could not select build driver", None)
        ]

    def test_no_supported_driver_records_the_requested_targets(self) -> None:
        """An auto run that finds no driver still records what was requested."""
        observer = _RecordingObserver()

        result = self._selected(observer, select_driver=mock.Mock(return_value=None))

        assert result is None
        selections = typ.cast(
            "list[dict[str, object]]", _events_of(observer, "quality_selected")
        )
        assert len(selections) == 1, (
            f"expected the request list to be published but got {selections!r}"
        )
        assert selections[0]["targets_requested"] == [
            "check-fmt",
            "lint",
            "typecheck",
        ]
        assert selections[0]["targets_run"] == []
        assert _events_of(observer, "gate_decided") == [
            ("quality_checks", "skip", "no supported build driver available", None)
        ]

    def test_no_requested_target_ran_is_recorded_as_a_skip(self) -> None:
        """A stop where none of the requested targets ran is not a pass.

        The driver declares a target the change never requested, so no command
        runs. Recording "quality checks passed" would claim checks that never
        executed; the gate is recorded as a skip stating that instead.
        """
        observer = _RecordingObserver()

        result = self._selected(
            observer,
            select_driver=mock.Mock(return_value=_driver()),
            targets={"release"},
        )

        assert result is None, "no block may be emitted"
        assert _events_of(observer, "gate_decided") == [
            (
                "quality_checks",
                "skip",
                "no requested target is declared by the build driver",
                None,
            )
        ], "the fixed pass text must not be used when no command ran"
        assert _telemetry_events(observer) == ["quality_gate_skip"]


# ---------------------------------------------------------------------------
# Repository facts
# ---------------------------------------------------------------------------


class _ScriptedFactsSource:
    """Repository-fact source answering from prepared values.

    ``branches`` and ``heads`` are consumed in call order, so a test can model
    the checkout moving between the initial read and the recheck around the
    unpushed query.
    """

    def __init__(  # ruff:ignore[too-many-arguments]  # each scripted answer is a distinct input.
        self,
        *,
        branches: list[str | None] | None = None,
        heads: list[str | None] | None = None,
        dirty: bool | None = False,
        unpushed: bool | None = False,
        branch_error: HookError | None = None,
        unpushed_error: HookError | None = None,
    ) -> None:
        """Store the scripted answers, consuming iterables as they are read."""
        # The unpushed binding reads the checkout twice: once for the record
        # and once to prove it did not move during the query.
        self._branches = iter(branches or ["feature", "feature"])
        self._heads = iter(heads or ["head", "head"])
        self._dirty = dirty
        self._unpushed = unpushed
        self._branch_error = branch_error
        self._unpushed_error = unpushed_error

    def current_branch(self, repo: Path) -> tuple[str | None, HookError | None]:
        """Return the next scripted branch answer and its failure."""
        return next(self._branches), self._branch_error

    def head_commit(self, repo: Path) -> tuple[str | None, HookError | None]:
        """Return the next scripted commit answer."""
        return next(self._heads), None

    def has_uncommitted_changes(
        self, repo: Path
    ) -> tuple[bool | None, HookError | None]:
        """Return the scripted dirty answer."""
        return self._dirty, None

    def is_ahead_of_upstream(
        self, repo: Path, upstream_ref: str
    ) -> tuple[bool | None, HookError | None]:
        """Return the scripted ahead-of-upstream answer and its failure."""
        return self._unpushed, self._unpushed_error


class TestRepositoryFacts:
    """The log-only facts are collected through the observer seam."""

    def test_facts_are_reported_for_an_observing_run(self, tmp_path: Path) -> None:
        """A recorder-backed observer receives the observed facts."""
        observer = _RecordingObserver()
        facts = GitFacts(
            primary_remote="origin",
            upstream_ref="origin/feature",
            pr_base_local_ref=None,
            local_base_commit="base",
            three_way_merge_is_configured=False,
            branch="feature",
        )

        facts_mod.observe_repository_facts(
            observer,
            _ScriptedFactsSource(),
            REPO,
            "origin/main",
            facts,
        )

        observations = _events_of(observer, "repository_observed")
        assert len(observations) == 1
        fields = typ.cast("dict[str, object]", observations[0])
        assert fields["branch"] == "feature"
        assert fields["head"] == "head"
        assert fields["dirty"] is False
        assert fields["unpushed"] is False

    def test_a_no_op_observer_skips_the_queries(self) -> None:
        """A run with logging disabled does not pay for the facts."""
        facts_mod.observe_repository_facts(
            NullObserver(_no_warnings),
            UnusedRepositoryFacts(),
            REPO,
            "origin/main",
            None,
        )

    def test_an_os_error_is_reported_as_a_bounded_failure(self) -> None:
        """A query that cannot start is recorded, never raised."""

        class _FailingSource(_ScriptedFactsSource):
            """Source whose first query cannot start a process."""

            def current_branch(self, repo: Path) -> tuple[str | None, HookError | None]:
                """Raise the launch failure the observation must contain."""
                message = "boom"
                raise OSError(message)

        observer = _RecordingObserver()
        facts_mod.observe_repository_facts(
            observer, _FailingSource(), REPO, "origin/main", None
        )

        fields = typ.cast(
            "dict[str, object]", _events_of(observer, "repository_observed")[0]
        )
        assert fields["error"] == facts_mod.OBSERVATION_FAILED
        assert "boom" not in json.dumps(fields), "the failure text must not be kept"

    def test_a_failed_query_is_reported_as_a_bounded_failure(self) -> None:
        """A query that answers with an error withholds only its own fact.

        The value and its ``HookError`` arrive together, so the observation
        names one bounded failure while the answers that did succeed are still
        recorded. The error text carries Git diagnostics, so it is never
        written; the decision is untouched.
        """
        observer = _RecordingObserver()
        failure = HookError("current_branch", "fatal: not a git repository")

        facts_mod.observe_repository_facts(
            observer,
            _ScriptedFactsSource(branches=[None, "feature"], branch_error=failure),
            REPO,
            "origin/main",
            None,
        )

        fields = typ.cast(
            "dict[str, object]", _events_of(observer, "repository_observed")[0]
        )
        assert fields["error"] == facts_mod.OBSERVATION_FAILED
        assert fields["branch"] is None, "a failed fact is withheld"
        assert fields["head"] == "head", "a fact that answered is still recorded"
        assert fields["dirty"] is False
        assert "not a git repository" not in json.dumps(fields), (
            "the Git diagnostic must never reach the durable record"
        )

    def test_a_failed_unpushed_query_is_reported_and_withheld(self) -> None:
        """A failed ahead-of-upstream query leaves the count unknown."""
        observer = _RecordingObserver()

        facts_mod.observe_repository_facts(
            observer,
            _ScriptedFactsSource(
                unpushed=None,
                unpushed_error=HookError("has_unpushed_commits", "rev-list failed"),
            ),
            REPO,
            "origin/main",
            GitFacts(
                primary_remote="origin",
                upstream_ref="origin/feature",
                pr_base_local_ref=None,
                local_base_commit="base",
                three_way_merge_is_configured=False,
                branch="feature",
            ),
        )

        fields = typ.cast(
            "dict[str, object]", _events_of(observer, "repository_observed")[0]
        )
        assert fields["error"] == facts_mod.OBSERVATION_FAILED
        assert fields["unpushed"] is None
        assert "rev-list failed" not in json.dumps(fields)


class TestUnpushedFactBinding:
    """The unpushed count describes the branch and commit the record reports."""

    facts = GitFacts(
        primary_remote="origin",
        upstream_ref="origin/feature",
        pr_base_local_ref=None,
        local_base_commit="base",
        three_way_merge_is_configured=False,
        branch="feature",
    )

    def _observe(
        self,
        observer: _RecordingObserver,
        *,
        branches: list[str | None],
        heads: list[str | None],
        unpushed: bool | None = True,
    ) -> dict[str, object]:
        """Run one observation with a scripted branch, head, and unpushed answer."""
        facts_mod.observe_repository_facts(
            observer,
            _ScriptedFactsSource(branches=branches, heads=heads, unpushed=unpushed),
            REPO,
            "origin/main",
            self.facts,
        )
        return typ.cast(
            "dict[str, object]", _events_of(observer, "repository_observed")[0]
        )

    def test_the_count_is_kept_when_the_checkout_is_stable(self) -> None:
        """A stable branch and commit keep the count the query reported."""
        observer = _RecordingObserver()

        fields = self._observe(
            observer,
            branches=["feature", "feature"],
            heads=["head", "head"],
        )

        assert fields["unpushed"] is True
        assert fields["branch"] == "feature"
        assert fields["head"] == "head"

    def test_the_count_is_withheld_when_the_branch_moves(self) -> None:
        """A checkout that moves mid-query withholds the value.

        The count would describe the newer branch while the record names the
        older one, so it is withheld rather than reported beside the wrong
        branch.
        """
        observer = _RecordingObserver()

        fields = self._observe(
            observer,
            branches=["feature", "other"],
            heads=["head", "head"],
        )

        assert fields["unpushed"] is None, (
            "a count read across a branch change must be withheld"
        )
        assert fields["branch"] == "feature", "the recorded branch is unchanged"

    def test_the_count_is_withheld_when_the_commit_moves(self) -> None:
        """A commit that moves mid-query withholds the value.

        The clone analogy: the record reports one commit, but the count was
        computed against a ``HEAD`` that has since advanced.
        """
        observer = _RecordingObserver()

        fields = self._observe(
            observer,
            branches=["feature", "feature"],
            heads=["head", "moved"],
        )

        assert fields["unpushed"] is None, (
            "a count read across a HEAD change must be withheld"
        )
        assert fields["head"] == "head", "the recorded commit is unchanged"

    def test_a_withheld_count_never_changes_the_other_facts(self) -> None:
        """The observation stays best-effort: only the count is withheld."""
        observer = _RecordingObserver()

        fields = self._observe(
            observer,
            branches=["feature", "other"],
            heads=["head", "moved"],
        )

        assert fields["unpushed"] is None
        assert fields["repo_root"] == str(REPO)
        assert fields["base_ref"] == "origin/main"
        assert fields["dirty"] is False


# ---------------------------------------------------------------------------
# End-to-end boundary
# ---------------------------------------------------------------------------


class TestRunStopChecksLogging:
    """The composed boundary records a complete run."""

    def test_a_clean_run_records_allow_and_every_reached_gate(
        self, tmp_path: Path
    ) -> None:
        """A run with no changed targets allows and records the gates it saw."""
        log = _make_log(_settings(tmp_path))
        state = state_mod.HookState()
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            config=Config(
                gate_uncommitted_changes=False,
                gate_unpushed_commits=False,
                gate_pr_rebase=False,
            ),
        )
        with (
            mock.patch.object(
                pipeline_mod,
                "prepare_run_stop_checks",
                return_value=state_mod.PreparationReady(state=state, repo=REPO),
            ),
            mock.patch.object(
                pipeline_mod,
                "run_quality_checks_if_needed",
                return_value=None,
            ),
            mock.patch.object(
                pipeline_mod,
                "run_branch_state_gates",
                return_value=0,
            ),
            mock.patch.object(pipeline_mod, "observe_repository_facts"),
        ):
            rc = pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                options,
                _no_upstream_source,
                branch_gate_ports=gate_ports(),
                repository_facts_source=_no_facts_source,
                warn=_no_warnings,
                invocation=InvocationLog(recorder=log),
            )
        log.finish()

        assert rc == 0
        finish = _finish_record(tmp_path / "state")
        assert finish["decision"] == "allow"
        assert finish["exit_code"] == 0
        stages = typ.cast("dict[str, int]", finish["stages"])
        assert {"prepare", "quality_checks", "branch_state_gates"} <= set(stages)

    def test_a_preparation_block_records_its_reason(self, tmp_path: Path) -> None:
        """A preparation failure records the fixed reason, not its message."""
        log = _make_log(_settings(tmp_path))
        error = HookError("ensure_base_ref", "git fetch origin failed: secret path")
        blocked_state = dataclasses.replace(state_mod.HookState(), ok=False)
        options = state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
        with (
            mock.patch.object(
                pipeline_mod,
                "prepare_run_stop_checks",
                return_value=state_mod.PreparationBlock(
                    state=blocked_state, error=error
                ),
            ),
            mock.patch.object(pipeline_mod, "block_and_print", return_value=0),
            mock.patch.object(pipeline_mod, "observe_repository_facts"),
        ):
            pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                options,
                _no_upstream_source,
                branch_gate_ports=gate_ports(),
                repository_facts_source=_no_facts_source,
                warn=_no_warnings,
                invocation=InvocationLog(recorder=log),
            )
        log.finish()

        finish = _finish_record(tmp_path / "state")
        assert finish["decision"] == "block"
        assert finish["block_reason"] is not None
        assert "secret path" not in json.dumps(finish), (
            "a user-facing message must never reach the durable record"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _driver() -> BuildDriver:
    """Return the Make build driver used by the quality tests."""
    from post_turn_quality_stop_hook.driver import BuildDriver

    return BuildDriver(
        "make",
        "make",
        "Makefile",
        selection_reason="selected by configuration",
    )


def _netsuke_driver() -> BuildDriver:
    """Return the Netsuke build driver used by the generation tests."""
    from post_turn_quality_stop_hook.driver import BuildDriver

    return BuildDriver("netsuke", "netsuke", "Netsukefile")
