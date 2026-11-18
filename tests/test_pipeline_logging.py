"""Decision-log integration tests for the stop-check pipeline.

The tests cover the records a prepared and completed run persists: gate
outcomes and reasons, repository facts, stage timings, commands and the final
decision. Common preparation patches are supplied by fixtures, so each test
states only the values it changes.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
import typing as typ
from pathlib import Path
from unittest import mock

import pytest
from hook_support import make_repository

from post_turn_quality_stop_hook import decision_log as log_mod
from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import git as git_mod
from post_turn_quality_stop_hook import github as github_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook.decision_log import DecisionLogSettings
from post_turn_quality_stop_hook.git import ExcludedPath, RepositoryView
from post_turn_quality_stop_hook.git_facts import GitFacts
from post_turn_quality_stop_hook.recording import InvocationLog

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from post_turn_quality_stop_hook.git import FetchObserver

REPO = Path("/fake/repo")

# A run started this far in the past must report at least this much startup
# time; the margin keeps the assertion stable on a loaded machine.
STARTUP_STAGE_MIN_MS = 200

_NO_FACTS = GitFacts(
    primary_remote=None,
    upstream_ref=None,
    pr_base_local_ref=None,
    local_base_commit=None,
    three_way_merge_is_configured=False,
)


def _decision_log(tmp_path: Path) -> log_mod.DecisionLog:
    """Build a decision log writing beneath the test's temporary directory."""
    return log_mod.DecisionLog(
        DecisionLogSettings(state_home=tmp_path / "state"),
        log_mod.InvocationContext(
            correlation={"session_id": "session-1"},
            start_dir=REPO,
            base_ref="origin/main",
        ),
    )


def _decision_records(tmp_path: Path) -> list[dict[str, object]]:
    """Read every record from the test's decision-log files."""
    log_dir = log_mod.log_directory(tmp_path / "state")
    return [
        typ.cast("dict[str, object]", json.loads(line))
        for path in sorted(log_dir.glob("hook-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _finish_record(tmp_path: Path) -> dict[str, object]:
    """Return the run's single finish record."""
    return next(
        record
        for record in _decision_records(tmp_path)
        if record["record_type"] == "finish"
    )


def _finish_after_gate(state: state_mod.HookState, log: log_mod.DecisionLog) -> None:
    """Drain the gate outcomes ``run_stop_checks`` would, then finish.

    A test that drives one gate directly does not pass through the
    orchestrator, which is the only place a decision log is written to. Doing
    the drain here keeps the test about what the gate decided.
    """
    state.gates.record_into(state.decision_log)
    log.finish()


@dataclasses.dataclass(frozen=True, slots=True)
class _PrBaseBranch:
    """One branch of the PR-base comparison and the calls it should make.

    ``calls`` pins the short circuit: a step that already failed must not be
    followed by the later comparisons.
    """

    name: str
    fetch: tuple[bool, str | None, bool]
    merge: tuple[str | None, str | None]
    ancestor: tuple[bool | None, str | None]
    expected: tuple[bool | None, str]
    calls: tuple[bool, bool, bool]


# ``summary.base_oid`` is ``"remote"`` in every case, so a merge base of
# ``"remote"`` is the case where the local base already equals the PR base.
_PR_BASE_BRANCHES = [
    _PrBaseBranch(
        name="fetch-failed",
        fetch=(False, "fetch failed", False),
        merge=("local", None),
        ancestor=(False, None),
        expected=(None, "could not fetch the pull request base"),
        calls=(True, False, False),
    ),
    _PrBaseBranch(
        name="merge-base-failed",
        fetch=(True, None, True),
        merge=(None, "merge base failed"),
        ancestor=(False, None),
        expected=(None, "could not compute the pull request merge base"),
        calls=(True, True, False),
    ),
    _PrBaseBranch(
        name="ancestry-failed",
        fetch=(True, None, True),
        merge=("local", None),
        ancestor=(None, "ancestry failed"),
        expected=(None, "could not compare the pull request base"),
        calls=(True, True, True),
    ),
    _PrBaseBranch(
        name="base-is-the-merge-base",
        fetch=(True, None, True),
        merge=("remote", None),
        ancestor=(True, None),
        expected=(False, "pull request base is not ahead"),
        calls=(True, True, False),
    ),
    _PrBaseBranch(
        name="base-ahead",
        fetch=(True, None, True),
        merge=("local", None),
        ancestor=(True, None),
        expected=(True, "pull request base is ahead"),
        calls=(True, True, True),
    ),
]


@pytest.fixture
def prepare_mocks() -> cabc.Iterator[None]:
    """Patch the Git facts every pipeline preparation reads."""
    with (
        mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
        mock.patch.object(pipeline_mod, "collect_git_facts", return_value=_NO_FACTS),
        mock.patch.object(
            pipeline_mod, "current_branch", return_value=("feature", None)
        ),
        mock.patch.object(pipeline_mod, "head_commit", return_value=("abc123", None)),
        mock.patch.object(
            pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
        ),
        mock.patch.object(pipeline_mod, "merge_base", return_value=("base123", None)),
        mock.patch("shutil.which", return_value="/usr/bin/git"),
    ):
        yield


@pytest.fixture
def clean_repository() -> cabc.Iterator[None]:
    """Report a clean tree with no changed files."""
    with (
        mock.patch.object(
            git_mod,
            "has_uncommitted_changes",
            return_value=git_mod.QueryResult(value=False),
        ),
        mock.patch.object(
            git_mod, "changed_files", return_value=git_mod.QueryResult([])
        ),
    ):
        yield


class TestDecisionLogIntegration:
    """The pipeline records gate outcomes without changing hook output."""

    driver = driver_mod.BuildDriver("make", "make", "Makefile")

    def test_allowed_run_records_every_gate_and_reason(
        self,
        tmp_path: Path,
        prepare_mocks: None,
        clean_repository: None,
    ) -> None:
        """A stop is joinable to its transcript and records all four gates."""
        log = _decision_log(tmp_path)
        rc = pipeline_mod.run_stop_checks(
            REPO,
            "origin/main",
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            invocation=InvocationLog(recorder=log),
        )
        log.finish()

        assert rc == 0, f"expected rc 0 but got {rc!r}"
        finish = _finish_record(tmp_path)
        assert finish["decision"] == "allow"
        assert finish["session_id"] == "session-1"
        gates = typ.cast("list[dict[str, object]]", finish["gates"])
        assert [gate["gate"] for gate in gates] == list(log_mod.GATE_ORDER)
        assert all(gate["reason"] for gate in gates), (
            f"every gate needs a reason but got {gates!r}"
        )
        assert [gate["outcome"] for gate in gates] == ["skip", "pass", "pass", "skip"]
        repository = typ.cast("dict[str, object]", finish["repository"])
        assert repository["branch"] == "feature"
        assert repository["head"] == "abc123"
        assert repository["dirty"] is False
        assert "prepare" in typ.cast("dict[str, object]", finish["stages"])

    def test_blocked_run_keeps_stdout_and_omits_command_output(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        prepare_mocks: None,
    ) -> None:
        """A blocking stop logs the gate failure without captured output."""
        log = _decision_log(tmp_path)
        failure = {
            "kind": "code",
            "cmd": "make --no-print-directory check-fmt lint typecheck",
            "exit_code": 1,
            "stdout": "SECRET STDOUT",
            "stderr": "SECRET STDERR",
        }
        with (
            mock.patch.object(
                git_mod,
                "has_uncommitted_changes",
                return_value=git_mod.QueryResult(value=False),
            ),
            mock.patch.object(
                git_mod,
                "changed_files",
                return_value=git_mod.QueryResult(["src/foo.py"]),
            ),
            mock.patch.object(
                pipeline_mod,
                "select_build_driver",
                return_value=(self.driver, None),
            ),
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value=({"check-fmt", "lint", "typecheck"}, None, 0),
            ),
            mock.patch.object(pipeline_mod, "run_build_targets", return_value=failure),
        ):
            rc = pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                invocation=InvocationLog(recorder=log),
            )
        log.finish()

        payload = json.loads(capsys.readouterr().out)
        assert payload["decision"] == "block", "stdout must keep the block payload"
        assert rc == 0, f"expected the hook contract to return 0 but got {rc!r}"
        finish = _finish_record(tmp_path)
        assert finish["decision"] == "block"
        assert finish["block_reason"] == "quality checks failed: code exited 1"
        gates = typ.cast("list[dict[str, object]]", finish["gates"])
        assert gates[0]["outcome"] == "block"
        assert gates[0]["reason"] == "quality checks failed: code exited 1"
        assert [gate["outcome"] for gate in gates[1:]] == [
            "not_reached",
            "not_reached",
            "not_reached",
        ]
        logged = json.dumps(_decision_records(tmp_path))
        assert "SECRET" not in logged, "command output must never reach the log"

    def test_target_enumeration_failure_keeps_output_out_of_the_log(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        prepare_mocks: None,
    ) -> None:
        """A netsuke failure keeps its output in the response, not the log."""
        log = _decision_log(tmp_path)
        with (
            mock.patch.object(
                git_mod,
                "has_uncommitted_changes",
                return_value=git_mod.QueryResult(value=False),
            ),
            mock.patch.object(
                git_mod,
                "changed_files",
                return_value=git_mod.QueryResult(["src/foo.py"]),
            ),
            mock.patch.object(
                pipeline_mod,
                "select_build_driver",
                return_value=(self.driver, None),
            ),
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value=(None, "SECRET OUTPUT", 1),
            ),
        ):
            rc = pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                invocation=InvocationLog(recorder=log),
            )
        log.finish()

        payload = json.loads(capsys.readouterr().out)
        assert payload["decision"] == "block"
        assert "SECRET OUTPUT" in payload["reason"], (
            "the response keeps the diagnostic detail"
        )
        assert rc == 0, f"expected the hook contract to return 0 but got {rc!r}"
        finish = _finish_record(tmp_path)
        assert finish["decision"] == "block"
        assert "SECRET" not in json.dumps(finish), (
            "command output must never reach the log"
        )
        gates = typ.cast("list[dict[str, object]]", finish["gates"])
        assert gates[0]["reason"] == "could not enumerate build targets"

    def test_driver_selection_failure_keeps_the_request_out_of_the_log(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        prepare_mocks: None,
    ) -> None:
        """A rejected driver name stays in the response, not the log."""
        log = _decision_log(tmp_path)
        error = "Unsupported build driver 'SECRET-DRIVER'. Use make, netsuke."
        with (
            mock.patch.object(
                git_mod,
                "has_uncommitted_changes",
                return_value=git_mod.QueryResult(value=False),
            ),
            mock.patch.object(
                git_mod,
                "changed_files",
                return_value=git_mod.QueryResult(["src/foo.py"]),
            ),
            mock.patch.object(
                pipeline_mod,
                "select_build_driver",
                return_value=(None, error),
            ),
        ):
            rc = pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                invocation=InvocationLog(recorder=log),
            )
        log.finish()

        payload = json.loads(capsys.readouterr().out)
        assert "SECRET-DRIVER" in payload["reason"], (
            "the response keeps the requested driver name"
        )
        assert rc == 0, f"expected the hook contract to return 0 but got {rc!r}"
        finish = _finish_record(tmp_path)
        assert finish["decision"] == "block"
        assert "SECRET" not in json.dumps(finish), (
            "an environment-provided driver name must never reach the log"
        )
        assert finish["block_reason"] == "could not select build driver"
        gates = typ.cast("list[dict[str, object]]", finish["gates"])
        assert gates[0]["reason"] == "could not select build driver"

    def test_pr_rebase_undetermined_outcome_is_a_skip(self, tmp_path: Path) -> None:
        """An undeterminable PR base is recorded as a skip, never a pass."""
        log = _decision_log(tmp_path)
        state = state_mod.HookState(
            git_facts=GitFacts(
                primary_remote="origin",
                upstream_ref=None,
                pr_base_local_ref=None,
                local_base_commit=None,
                three_way_merge_is_configured=False,
            ),
            decision_log=log,
        )
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod,
                "remote_url",
                return_value=("https://github.com/o/r.git", None),
            ),
            mock.patch.object(pipeline_mod, "lookup_pr", return_value=None),
        ):
            rc = pipeline_mod.pr_rebase_check(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )
        _finish_after_gate(state, log)

        assert rc == 0
        finish = _finish_record(tmp_path)
        gates = typ.cast("list[dict[str, object]]", finish["gates"])
        pr_gate = next(gate for gate in gates if gate["gate"] == "pr_rebase")
        assert pr_gate["outcome"] == "skip"
        assert pr_gate["reason"] == "could not determine an open pull request"


class TestDecisionLogCoverage:
    """Startup, progress, and selection facts stay visible in the log."""

    def test_startup_time_is_recorded_as_a_finished_stage(self, tmp_path: Path) -> None:
        """The finish record covers work done before the log existed."""
        started = time.monotonic() - 0.2
        log = log_mod.DecisionLog(
            DecisionLogSettings(state_home=tmp_path / "state"),
            log_mod.InvocationContext(
                correlation={"session_id": "session-1"},
                start_dir=REPO,
                base_ref="origin/main",
            ),
            started=started,
        )
        log.finish()

        finish = _finish_record(tmp_path)
        stages = typ.cast("dict[str, int]", finish["stages"])
        assert stages["startup"] >= STARTUP_STAGE_MIN_MS, (
            f"startup must be timed from process start but got {stages!r}"
        )
        assert typ.cast("int", finish["duration_ms"]) >= stages["startup"]

    def test_netsuke_target_enumeration_is_recorded(self, tmp_path: Path) -> None:
        """``netsuke generate`` is recorded with its own exit code."""
        log = _decision_log(tmp_path)
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with mock.patch.object(
            pipeline_mod, "get_build_targets", return_value=({"check-fmt"}, None, 3)
        ):
            available, err = pipeline_mod._enumerate_targets(log, REPO, driver)
        log.finish()

        assert available == {"check-fmt"}
        assert err is None
        finish = _finish_record(tmp_path)
        commands = typ.cast("list[dict[str, object]]", finish["commands"])
        assert [
            (command["kind"], command["cmd"], command["exit_code"])
            for command in commands
        ] == [("targets", "netsuke generate", 3)]

    def test_make_target_enumeration_is_not_a_command(self, tmp_path: Path) -> None:
        """Reading a Makefile is local work, not a blocking tool invocation."""
        log = _decision_log(tmp_path)
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        with mock.patch.object(
            pipeline_mod, "get_build_targets", return_value=({"check-fmt"}, None, None)
        ):
            pipeline_mod._enumerate_targets(log, REPO, driver)
        log.finish()

        finish = _finish_record(tmp_path)
        assert finish["commands"] == []

    @pytest.mark.parametrize(
        ("fetch_exit_code", "expected_commands"),
        [
            (0, [("fetch", "git fetch", 0)]),
            (None, []),
        ],
        ids=["fetch-ran", "ref-already-present"],
    )
    def test_base_ref_fetch_is_recorded_only_when_it_runs(
        self,
        tmp_path: Path,
        fetch_exit_code: int | None,
        expected_commands: list[tuple[str, str, int]],
    ) -> None:
        """A fetch is recorded with its own exit code; a ref check is not."""
        log = _decision_log(tmp_path)

        def ensure(*_args: object, **kwargs: object) -> tuple[bool, str | None, bool]:
            """Report the ref as present, running a fetch only when asked."""
            if fetch_exit_code is None:
                return True, None, False
            observer = typ.cast("FetchObserver", kwargs["observer"])
            observer.fetch_finished(observer.fetch_started(), fetch_exit_code)
            return True, None, True

        with (
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "collect_git_facts", return_value=_NO_FACTS
            ),
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "head_commit", return_value=("abc123", None)
            ),
            mock.patch.object(
                git_mod,
                "has_uncommitted_changes",
                return_value=git_mod.QueryResult(value=False),
            ),
            mock.patch.object(pipeline_mod, "ensure_base_ref", side_effect=ensure),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("base123", None)
            ),
            mock.patch.object(
                git_mod, "changed_files", return_value=git_mod.QueryResult([])
            ),
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            preparation = pipeline_mod.prepare_run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                invocation=InvocationLog(recorder=log),
            )
        log.finish()

        assert preparation.ok
        finish = _finish_record(tmp_path)
        commands = typ.cast("list[dict[str, object]]", finish["commands"])
        assert [
            (command["kind"], command["cmd"], command["exit_code"])
            for command in commands
        ] == expected_commands

    def test_log_inside_worktree_is_excluded_from_repository_queries(
        self, tmp_path: Path
    ) -> None:
        """A state home inside the worktree cannot dirty the tree it logs."""
        repo = tmp_path / "repo"
        repo.mkdir()
        log = log_mod.DecisionLog(
            DecisionLogSettings(state_home=repo / ".state"),
            log_mod.InvocationContext(
                correlation={}, start_dir=repo, base_ref="origin/main"
            ),
        )
        with (
            mock.patch.object(pipeline_mod, "repo_root", return_value=(repo, None)),
            mock.patch.object(
                pipeline_mod, "collect_git_facts", return_value=_NO_FACTS
            ),
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "head_commit", return_value=("abc123", None)
            ),
            mock.patch.object(
                git_mod,
                "has_uncommitted_changes",
                return_value=git_mod.QueryResult(value=False),
            ) as dirty_mock,
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("base123", None)
            ),
            mock.patch.object(
                git_mod, "changed_files", return_value=git_mod.QueryResult([])
            ) as files_mock,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            preparation = pipeline_mod.prepare_run_stop_checks(
                repo,
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                invocation=InvocationLog(recorder=log),
            )
        log.finish()

        assert preparation.ok
        log_dir = log_mod.log_directory(repo / ".state")
        written = [path.name for path in log_dir.iterdir()]
        assert len(written) == 1, f"expected one daily file but got {written!r}"
        expected = (ExcludedPath(log_dir, written[0]),)
        assert dirty_mock.call_args.kwargs["exclude"] == expected
        assert files_mock.call_args.kwargs["exclude"] == expected

    def test_branch_state_gates_record_individual_stages(self, tmp_path: Path) -> None:
        """Each branch-state gate is timed separately so a timeout is locatable."""
        log = _decision_log(tmp_path)
        state = state_mod.HookState(git_facts=_NO_FACTS, decision_log=log)
        with (
            mock.patch.object(
                pipeline_mod,
                "uncommitted_changes_gate",
                return_value=pipeline_mod.BranchStateGateDecision(
                    gate="uncommitted_changes", outcome="pass", reason="clean"
                ),
            ) as uncommitted_mock,
            mock.patch.object(
                pipeline_mod,
                "unpushed_commits_gate",
                return_value=pipeline_mod.BranchStateGateDecision(
                    gate="unpushed_commits", outcome="pass", reason="pushed"
                ),
            ),
            mock.patch.object(pipeline_mod, "pr_rebase_check", return_value=0),
        ):
            repository = RepositoryView(
                lambda: (
                    ExcludedPath(
                        log_mod.log_directory(tmp_path / "state"),
                        "hook-2026-11-06.jsonl",
                    ),
                )
            )
            rc = pipeline_mod.run_branch_state_gates(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                repository=repository,
            )
        log.finish()

        assert rc == 0
        assert uncommitted_mock.call_args.kwargs["repository"] is repository
        finish = _finish_record(tmp_path)
        stages = typ.cast("dict[str, int]", finish["stages"])
        assert {
            "gate_uncommitted_changes",
            "gate_unpushed_commits",
            "gate_pr_rebase",
        } <= set(stages)

    @pytest.mark.parametrize(
        ("ahead", "reason", "outcome"),
        [
            (False, "pull request base is not ahead", "pass"),
            (None, "could not fetch the pull request base", "skip"),
        ],
    )
    def test_pr_rebase_distinguishes_pass_from_undetermined(
        self,
        tmp_path: Path,
        ahead: bool | None,
        reason: str,
        outcome: str,
    ) -> None:
        """A determined not-ahead result passes; an unknown result skips."""
        log = _decision_log(tmp_path)
        state = state_mod.HookState(
            git_facts=GitFacts(
                primary_remote="origin",
                upstream_ref=None,
                pr_base_local_ref=None,
                local_base_commit=None,
                three_way_merge_is_configured=False,
            ),
            decision_log=log,
        )
        summary = github_mod.PullRequestSummary(
            number=1, base_branch="main", base_oid="remote"
        )
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod,
                "remote_url",
                return_value=("https://github.com/o/r.git", None),
            ),
            mock.patch.object(pipeline_mod, "lookup_pr", return_value=summary),
            mock.patch.object(
                pipeline_mod, "_pr_base_is_ahead", return_value=(ahead, reason)
            ),
            mock.patch.object(
                pipeline_mod, "get_build_targets", return_value=({"typecheck"}, None, 0)
            ),
        ):
            rc = pipeline_mod.pr_rebase_check(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )
        _finish_after_gate(state, log)

        assert rc == 0
        finish = _finish_record(tmp_path)
        gates = typ.cast("list[dict[str, object]]", finish["gates"])
        pr_gate = next(gate for gate in gates if gate["gate"] == "pr_rebase")
        assert pr_gate["outcome"] == outcome
        assert pr_gate["reason"] == reason

    def test_pr_base_fetch_records_the_command_exit_code(self, tmp_path: Path) -> None:
        """The PR-base fetch is recorded with the fetch's own exit code."""
        log = _decision_log(tmp_path)
        summary = github_mod.PullRequestSummary(
            number=1, base_branch="main", base_oid="remote"
        )

        def ensure(*_args: object, **kwargs: object) -> tuple[bool, str | None, bool]:
            """Report a successful fetch with exit code 7 through the observer."""
            observer = typ.cast("FetchObserver", kwargs["observer"])
            observer.fetch_finished(observer.fetch_started(), 7)
            return True, None, True

        with (
            mock.patch.object(
                pipeline_mod, "ensure_remote_branch_ref", side_effect=ensure
            ),
            mock.patch.object(pipeline_mod, "merge_base", return_value=("local", None)),
            mock.patch.object(pipeline_mod, "is_ancestor", return_value=(False, None)),
        ):
            ahead, reason = pipeline_mod._pr_base_is_ahead(
                REPO, "origin", summary, recorder=log
            )
        log.finish()

        assert ahead is False
        assert reason == "pull request base is not ahead"
        finish = _finish_record(tmp_path)
        commands = typ.cast("list[dict[str, object]]", finish["commands"])
        assert [
            (command["kind"], command["cmd"], command["exit_code"])
            for command in commands
        ] == [("fetch", "git fetch", 7)]

    @pytest.mark.parametrize(
        "branch", _PR_BASE_BRANCHES, ids=lambda branch: branch.name
    )
    def test_pr_base_is_ahead_reports_each_outcome(self, branch: _PrBaseBranch) -> None:
        """Each Git failure is undetermined, not "not ahead".

        ``ensure_remote_branch_ref``, ``merge_base`` and ``is_ancestor`` all
        report failure with ``None``. Returning ``False`` for any of them
        would let a stale branch pass the rebase gate, so every branch asserts
        the answer and which collaborators were consulted.
        """
        summary = github_mod.PullRequestSummary(
            number=1, base_branch="main", base_oid="remote"
        )
        with (
            mock.patch.object(
                pipeline_mod, "ensure_remote_branch_ref", return_value=branch.fetch
            ) as fetch_mock,
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=branch.merge
            ) as merge_mock,
            mock.patch.object(
                pipeline_mod, "is_ancestor", return_value=branch.ancestor
            ) as ancestor_mock,
        ):
            answer = pipeline_mod._pr_base_is_ahead(REPO, "origin", summary)

        assert answer == branch.expected, (
            f"PR case {branch.name!r} must answer {branch.expected!r}"
            f" but answered {answer!r}"
        )
        observed_calls = (
            fetch_mock.called,
            merge_mock.called,
            ancestor_mock.called,
        )
        assert observed_calls == branch.calls, (
            f"PR case {branch.name!r} must consult (fetch, merge-base,"
            f" is-ancestor) as {branch.calls!r} but consulted {observed_calls!r}"
        )

    @pytest.mark.parametrize(
        "branch",
        [case for case in _PR_BASE_BRANCHES if case.expected[0] is None],
        ids=lambda branch: branch.name,
    )
    def test_an_undetermined_pr_base_skips_the_gate(
        self, tmp_path: Path, branch: _PrBaseBranch
    ) -> None:
        """The real gate records a skip for every undetermined comparison."""
        log = _decision_log(tmp_path)
        state = state_mod.HookState(
            git_facts=GitFacts(
                primary_remote="origin",
                upstream_ref="origin/feature",
                pr_base_local_ref=None,
                local_base_commit="base",
                three_way_merge_is_configured=False,
            ),
            decision_log=log,
        )
        summary = github_mod.PullRequestSummary(
            number=1, base_branch="main", base_oid="remote"
        )
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod,
                "remote_url",
                return_value=("https://github.com/o/r.git", None),
            ),
            mock.patch.object(pipeline_mod, "lookup_pr", return_value=summary),
            mock.patch.object(
                pipeline_mod, "ensure_remote_branch_ref", return_value=branch.fetch
            ),
            mock.patch.object(pipeline_mod, "merge_base", return_value=branch.merge),
            mock.patch.object(
                pipeline_mod, "is_ancestor", return_value=branch.ancestor
            ),
        ):
            rc = pipeline_mod.pr_rebase_check(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )
        _finish_after_gate(state, log)

        assert rc == 0, (
            f"PR case {branch.name!r} must not block, but the gate returned {rc}"
        )
        finish = _finish_record(tmp_path)
        gates = typ.cast("list[dict[str, object]]", finish["gates"])
        pr_gate = next(gate for gate in gates if gate["gate"] == "pr_rebase")
        assert pr_gate["outcome"] == "skip", (
            f"PR case {branch.name!r} must skip, but recorded {pr_gate['outcome']!r}"
        )
        assert pr_gate["reason"] == branch.expected[1], (
            f"PR case {branch.name!r} must record {branch.expected[1]!r},"
            f" but recorded {pr_gate['reason']!r}"
        )

    @pytest.mark.parametrize(
        "error", [None, "Unsupported build driver 'netsuke'. Use make, netsuke."]
    )
    def test_requested_targets_are_recorded_without_a_driver(
        self, tmp_path: Path, error: str | None
    ) -> None:
        """A skip or block with no driver still records what was requested."""
        log = _decision_log(tmp_path)
        state = state_mod.HookState(changed_files=["src/foo.py"], decision_log=log)
        with mock.patch.object(
            pipeline_mod, "select_build_driver", return_value=(None, error)
        ):
            pipeline_mod.run_quality_checks_if_needed(
                state,
                REPO,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )
        log.finish()

        finish = _finish_record(tmp_path)
        quality = typ.cast("dict[str, object]", finish["quality"])
        assert quality["targets_requested"] == [
            "check-fmt",
            "lint",
            "typecheck",
        ], (
            f"with driver error {error!r} the requested targets must still be"
            f" recorded, but recorded {quality['targets_requested']!r}"
        )


class TestExclusionReporting:
    """The repository view carries an exclusion fallback without warning.

    Only the pipeline boundary owns standard error, so the view reports the
    fallback in its result and leaves the warning to its caller.
    """

    def test_changed_files_keeps_the_result_and_the_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The adapter preserves the exclusion and reports the fallback."""
        view = RepositoryView(lambda: (ExcludedPath(Path("/state")),))
        with mock.patch.object(
            git_mod,
            "changed_files",
            return_value=git_mod.QueryResult(
                ["a.py"], warning=git_mod._EXCLUSION_WARNING
            ),
        ) as files_mock:
            answer = view.changed_files(REPO, "abc123")

        assert answer.value == ["a.py"], (
            f"expected the adapter result but got {answer.value!r}"
        )
        assert answer.error is None, f"expected no error but got {answer.error!r}"
        assert answer.warning == git_mod._EXCLUSION_WARNING, (
            f"expected the exclusion warning but observed {answer.warning!r}"
        )
        assert capsys.readouterr().err == "", (
            "a query must not write the warning to standard error"
        )
        files_mock.assert_called_once_with(
            REPO, "abc123", exclude=(ExcludedPath(Path("/state")),)
        )

    def test_dirty_keeps_the_result_and_the_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A clean result from the adapter is used unchanged."""
        view = RepositoryView(lambda: (ExcludedPath(Path("/state")),))
        with mock.patch.object(
            git_mod,
            "has_uncommitted_changes",
            return_value=git_mod.QueryResult(
                value=False, warning=git_mod._EXCLUSION_WARNING
            ),
        ) as dirty_mock:
            answer = view.has_uncommitted_changes(REPO)

        assert answer.value is False, (
            f"expected the adapter result but got {answer.value!r}"
        )
        assert answer.error is None, f"expected no error but got {answer.error!r}"
        assert answer.warning == git_mod._EXCLUSION_WARNING, (
            f"expected the exclusion warning but observed {answer.warning!r}"
        )
        assert capsys.readouterr().err == "", (
            "a query must not write the warning to standard error"
        )
        dirty_mock.assert_called_once_with(
            REPO, exclude=(ExcludedPath(Path("/state")),)
        )

    def test_an_unresolved_supplier_reports_the_fallback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A supplier that cannot answer degrades to no exclusions plus warning."""
        view = RepositoryView(lambda: None)
        with mock.patch.object(
            git_mod, "changed_files", return_value=git_mod.QueryResult(["a.py"])
        ) as files_mock:
            answer = view.changed_files(REPO, "abc123")

        assert answer.value == ["a.py"], (
            f"expected the query result but observed {answer.value!r}"
        )
        assert answer.warning == git_mod._EXCLUSION_WARNING, (
            "an unresolved exclusion must not be silently dropped;"
            f" observed warning {answer.warning!r}"
        )
        assert capsys.readouterr().err == ""
        files_mock.assert_called_once_with(REPO, "abc123", exclude=())

    def test_a_raising_supplier_reports_the_fallback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A supplier that raises must not escape into the gate."""

        def broken() -> tuple[ExcludedPath, ...] | None:
            """Fail the way a substituted sink might."""
            message = "supplier failure"
            raise RuntimeError(message)

        view = RepositoryView(broken)
        with mock.patch.object(
            git_mod,
            "has_uncommitted_changes",
            return_value=git_mod.QueryResult(value=False),
        ):
            answer = view.has_uncommitted_changes(REPO)

        assert answer.value is False, (
            f"expected the raising supplier to keep the query result,"
            f" but observed {answer.value!r}"
        )
        assert answer.warning == git_mod._EXCLUSION_WARNING, (
            f"expected the exclusion warning but observed {answer.warning!r}"
        )
        assert capsys.readouterr().err == ""

    def test_git_error_is_returned(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A missing result is a Git failure, not an exclusion warning."""
        view = RepositoryView(lambda: (ExcludedPath(Path("/state")),))
        with mock.patch.object(
            git_mod,
            "changed_files",
            return_value=git_mod.QueryResult(None, "git failed"),
        ) as files_mock:
            answer = view.changed_files(REPO, "abc123")

        assert answer.value is None, (
            f"a failed query must yield no value, but observed {answer.value!r}"
        )
        assert answer.error == "git failed", (
            f"expected the Git error but observed error {answer.error!r}"
            f" and warning {answer.warning!r}"
        )
        assert answer.warning is None, (
            f"a Git failure is not an exclusion warning, but observed"
            f" warning {answer.warning!r}"
        )
        assert capsys.readouterr().err == ""
        files_mock.assert_called_once_with(
            REPO, "abc123", exclude=(ExcludedPath(Path("/state")),)
        )

    def test_a_failing_sink_warns_at_the_gate_and_excludes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A sink that cannot answer reports the failure, not an empty answer.

        ``excluded_paths`` returns ``None`` so the gate boundary can warn that
        the exclusion degraded. Returning an empty tuple would silently let the
        run's own daily file count as a repository change.
        """

        class _UnanswerableSink:
            """Sink whose exclusion query always fails."""

            def write(self, record: typ.Mapping[str, object]) -> None:
                """Accept the start record without persisting it."""
                del record

            def prune(self, today: object, retention_days: int) -> None:
                """Prune nothing."""
                del today, retention_days

            def excluded_paths(self) -> tuple[ExcludedPath, ...] | None:
                """Fail the way a broken storage backend might."""
                message = "sink failure"
                raise RuntimeError(message)

        log = log_mod.DecisionLog(
            DecisionLogSettings(state_home=tmp_path / "state"),
            log_mod.InvocationContext(
                correlation={}, start_dir=REPO, base_ref="origin/main"
            ),
            deps=log_mod.DecisionLogDeps(sink=_UnanswerableSink()),
        )
        view = RepositoryView(log.excluded_paths)
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                git_mod,
                "has_uncommitted_changes",
                return_value=git_mod.QueryResult(value=False),
            ),
        ):
            decision = pipeline_mod.uncommitted_changes_gate(
                REPO,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                repository=view,
            )

        log.finish()

        assert log.excluded_paths() is None, (
            "a failing sink must not report an empty exclusion set"
        )
        assert decision.outcome == "pass", "the decision must not depend on logging"
        assert "Could not fully resolve the decision-log exclusion" in (
            capsys.readouterr().err
        )

    def test_a_sink_that_breaks_after_the_start_record_blocks_rather_than_allows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real start record in the worktree fails closed when exclusions fail.

        The record is genuinely persisted inside the repository, so once the
        exclusion query breaks the daily file is an untracked change the gate
        can see. A no-logging run on the same clean starting tree passes. The
        broken logging run may therefore only become stricter, never more
        permissive, because the dangerous direction is allowing a stop while
        the hook's own file sits unnoticed in the worktree.
        """
        _isolate_git(monkeypatch)
        repo = make_repository(tmp_path)
        state_home = repo / "state"
        options = state_mod.StopCheckOptions(always_fetch=False, max_out=12000)

        # The counterfactual runs first, on the same tree the logged run
        # starts from: no recorder, no exclusions, no daily file yet. Building
        # the log is what writes the start record, so it must come after.
        unlogged = pipeline_mod.uncommitted_changes_gate(
            repo, options, repository=RepositoryView()
        )
        log = log_mod.DecisionLog(
            DecisionLogSettings(state_home=state_home),
            log_mod.InvocationContext(
                correlation={}, start_dir=repo, base_ref="origin/main"
            ),
            deps=log_mod.DecisionLogDeps(
                sink=_RecordsThenBreaksExclusions(
                    log_mod.JsonlRecordSink(
                        log_mod.log_directory(state_home),
                        is_tracked=git_mod.is_tracked,
                    )
                )
            ),
        )
        logged = pipeline_mod.uncommitted_changes_gate(
            repo, options, repository=log.repository_view()
        )
        log.finish()

        assert unlogged.outcome == "pass", (
            "the clean starting tree must pass without logging,"
            f" but the gate decided {unlogged.outcome!r}"
        )
        assert logged.outcome == "block", (
            "a start record the exclusion cannot report must block,"
            f" but the gate decided {logged.outcome!r}"
        )
        assert log.excluded_paths() is None, (
            "the broken sink must report an unknown exclusion, not an empty one"
        )

    def test_a_genuine_untracked_sibling_blocks_despite_the_exclusion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A working exclusion hides the daily file, not a sibling beside it.

        The start record really is persisted under ``state/`` inside the
        worktree and really is excluded. An unrelated untracked file must still
        block, exactly as it does with logging disabled.
        """
        _isolate_git(monkeypatch)
        repo = make_repository(tmp_path)
        (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        state_home = repo / "state"
        options = state_mod.StopCheckOptions(always_fetch=False, max_out=12000)

        # Run the counterfactual before the log exists: constructing it writes
        # the start record, which is itself an untracked file.
        unlogged = pipeline_mod.uncommitted_changes_gate(
            repo, options, repository=RepositoryView()
        )
        log = log_mod.DecisionLog(
            DecisionLogSettings(state_home=state_home),
            log_mod.InvocationContext(
                correlation={}, start_dir=repo, base_ref="origin/main"
            ),
            deps=log_mod.DecisionLogDeps(
                sink=log_mod.JsonlRecordSink(
                    log_mod.log_directory(state_home),
                    is_tracked=git_mod.is_tracked,
                )
            ),
        )
        logged = pipeline_mod.uncommitted_changes_gate(
            repo, options, repository=log.repository_view()
        )
        excluded = log.excluded_paths()
        log.finish()

        assert excluded != (), (
            "the start record must really be excluded for this case to mean"
            " anything, but the sink excluded nothing"
        )
        assert unlogged.outcome == "block", (
            "the untracked sibling must block without logging,"
            f" but the gate decided {unlogged.outcome!r}"
        )
        assert logged.outcome == "block", (
            "the exclusion must not hide an untracked file beside the log,"
            f" but the gate decided {logged.outcome!r}"
        )


def _isolate_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real Git queries in this module free of user configuration."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


class _RecordsThenBreaksExclusions:
    """Sink that persists records, then cannot answer the exclusion query."""

    def __init__(self, inner: log_mod.JsonlRecordSink) -> None:
        """Wrap a real sink and start out able to answer the query."""
        self._inner = inner
        self._has_written = False

    def write(self, record: typ.Mapping[str, object]) -> None:
        """Persist the record through the real sink."""
        self._inner.write(record)
        self._has_written = True

    def prune(self, today: dt.date, retention_days: int) -> None:
        """Delegate retention to the real sink."""
        self._inner.prune(today, retention_days)

    def excluded_paths(self) -> tuple[ExcludedPath, ...] | None:
        """Answer until the start record exists, then fail.

        A storage backend that accepted the write and then broke is the case
        where the daily file exists but the run cannot say so.
        """
        if not self._has_written:
            return self._inner.excluded_paths()
        message = "sink failure"
        raise RuntimeError(message)
