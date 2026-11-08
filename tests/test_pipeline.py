"""Cover stop-check orchestration, branch-state and PR-rebase gates.

The tests also cover error handling and block-output formatting. The legacy
``compush_check`` helper is tested separately as a downstream-compatibility
API, not as an active gate.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import typing as typ
from pathlib import Path
from unittest import mock

if typ.TYPE_CHECKING:
    import collections.abc as cabc

import pytest
from hypothesis import given
from hypothesis import strategies as st

from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import execution as exec_mod
from post_turn_quality_stop_hook import formatting as formatting_mod
from post_turn_quality_stop_hook import github as github_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.git_facts import GitFacts

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess with the given return code and output."""
    return subprocess.CompletedProcess(
        args=["unit-test"], returncode=returncode, stdout=stdout, stderr=stderr
    )


REPO = Path("/fake/repo")

GIT_FACTS = GitFacts(
    primary_remote="origin",
    upstream_ref="origin/feature",
    pr_base_local_ref=None,
    local_base_commit=None,
    three_way_merge_is_configured=False,
)


def _enter_patchers(
    patches: cabc.Iterable[contextlib.AbstractContextManager[object]],
) -> contextlib.ExitStack:
    """Enter every patcher and return the stack that owns them."""
    stack = contextlib.ExitStack()
    for patcher in patches:
        stack.enter_context(patcher)
    return stack


def _command(
    kind: str = "code", exit_code: int = 0, cmd: str = "make lint"
) -> exec_mod.CommandResult:
    """Build a command result for a quality evaluation."""
    return {
        "kind": kind,
        "cmd": cmd,
        "exit_code": exit_code,
        "stdout": "" if exit_code == 0 else "boom",
        "stderr": "",
    }


def _evaluation(*, blocked: bool = False) -> state_mod.QualityEvaluation:
    """Build a passing or blocked quality evaluation for orchestration tests."""
    commands = (_command(exit_code=1),) if blocked else (_command(),)
    return state_mod.QualityEvaluation(
        categories={"code"},
        build_driver="make",
        targets_requested=("check-fmt", "lint", "typecheck"),
        targets_present=("check-fmt", "lint", "typecheck"),
        commands=commands,
    )


class TestCompushCheckCompatibility:
    """Compatibility behavior for downstream imports of ``compush_check``."""

    def test_compatibility_helper_remains_importable(self) -> None:
        """Downstream imports retain the historic public helper path."""
        from post_turn_quality_stop_hook.pipeline import compush_check

        assert callable(compush_check), (
            "The downstream compush_check compatibility helper must remain importable."
        )

    def test_dirty_tree_emits_commit_and_push_block(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A dirty tree retains the legacy commit-and-push reminder payload."""
        with (
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(True, None)
            ),
        ):
            result = pipeline_mod.compush_check(REPO)

        assert result == 0, (
            "Legacy compush_check must always return the hook pass code."
        )
        assert json.loads(capsys.readouterr().out) == {
            "decision": "block",
            "reason": "Please commit and push to origin/feature",
        }, "Dirty-tree compatibility payload must preserve its legacy wording."

    def test_dirty_tree_without_upstream_emits_legacy_fallback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Dirty work still receives the old fallback reminder without an upstream."""
        with (
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=(None, "no upstream")
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(True, None)
            ),
        ):
            result = pipeline_mod.compush_check(REPO)

        assert result == 0, "Legacy compush_check must pass on dirty-tree reminders."
        assert json.loads(capsys.readouterr().out) == {
            "decision": "block",
            "reason": "Please commit and push to origin (upstream not configured)",
        }, "Dirty-tree fallback payload must preserve its legacy wording."

    def test_ahead_branch_emits_push_only_block(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A clean but ahead branch retains the legacy push-only payload."""
        with (
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(
                pipeline_mod, "has_unpushed_commits", return_value=(True, None)
            ),
        ):
            result = pipeline_mod.compush_check(REPO)

        assert result == 0, "Legacy compush_check must pass on ahead-branch reminders."
        assert json.loads(capsys.readouterr().out) == {
            "decision": "block",
            "reason": "Please push committed changes to origin/feature",
        }, "Ahead-branch compatibility payload must preserve its legacy wording."

    def test_missing_upstream_on_clean_tree_is_silent(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without a clean-tree upstream there is no push status to report."""
        with (
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=(None, "no upstream")
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(pipeline_mod, "has_unpushed_commits") as mock_ahead,
        ):
            result = pipeline_mod.compush_check(REPO)

        assert result == 0, "Missing upstream must not fail the compatibility helper."
        mock_ahead.assert_not_called()
        assert capsys.readouterr().out == "", (
            "A clean tree without an upstream must not emit a compatibility block."
        )

    @pytest.mark.parametrize(
        ("dirty_result", "ahead_result"),
        [
            ((None, "git status failed"), (True, None)),
            ((False, None), (None, "git rev-list failed")),
        ],
        ids=("working-tree-query-error", "ahead-query-error"),
    )
    def test_git_query_errors_are_silent(
        self,
        capsys: pytest.CaptureFixture[str],
        dirty_result: tuple[bool | None, str | None],
        ahead_result: tuple[bool | None, str | None],
    ) -> None:
        """Uncertain Git state preserves the legacy silent-pass behavior."""
        with (
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=dirty_result
            ),
            mock.patch.object(
                pipeline_mod, "has_unpushed_commits", return_value=ahead_result
            ),
        ):
            result = pipeline_mod.compush_check(REPO)

        assert result == 0, "Git query errors must not fail the compatibility helper."
        assert capsys.readouterr().out == "", (
            "Git query errors must not emit an unverified compatibility block."
        )


BRANCH_NAME_STRATEGY = st.sampled_from((
    "main",
    "master",
    "release",
    "trunk",
    "stable",
    "develop",
    "feature",
))
REMOTE_NAME_STRATEGY = st.sampled_from(("origin", "upstream", "team/fork"))


def _protected_branch_case(
    value: tuple[str, set[str]],
) -> tuple[str, tuple[str, ...]]:
    """Add the selected branch to its generated protected branch set."""
    protected_branch, extra_branches = value
    configured = tuple(sorted({protected_branch, *extra_branches}))
    return protected_branch, configured


PROTECTED_BRANCH_SET_STRATEGY = st.tuples(
    BRANCH_NAME_STRATEGY, st.sets(BRANCH_NAME_STRATEGY, max_size=4)
).map(_protected_branch_case)


class TestRunStopChecksBranchStateGates:
    """Integration-level tests for branch-state gates in run_stop_checks()."""

    driver = driver_mod.BuildDriver("make", "make", "Makefile")

    def test_uncommitted_gate_triggers_after_success(self) -> None:
        """Quality pass + dirty tree -> uncommitted gate called."""
        with (
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(
                pipeline_mod, "changed_files", return_value=(["src/foo.py"], None)
            ),
            mock.patch.object(
                pipeline_mod, "select_build_driver", return_value=self.driver
            ),
            mock.patch.object(
                pipeline_mod, "evaluate_changes", return_value=_evaluation()
            ),
            mock.patch.object(pipeline_mod, "compush_check") as mock_compush,
            mock.patch.object(
                pipeline_mod,
                "uncommitted_changes_gate",
                return_value=pipeline_mod.BranchStateGateDecision(
                    gate="uncommitted_changes", outcome="block", payload={}
                ),
            ) as mock_uncommitted,
            mock.patch.object(pipeline_mod, "unpushed_commits_gate") as mock_unpushed,
            mock.patch.object(pipeline_mod, "pr_rebase_check") as mock_rebase,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            rc = pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                ),
            )
        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        mock_uncommitted.assert_called_once()
        mock_unpushed.assert_not_called()
        mock_rebase.assert_not_called()
        mock_compush.assert_not_called()

    def test_unpushed_gate_runs_after_clean_worktree(self) -> None:
        """Clean working tree -> unpushed gate is evaluated."""
        with (
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(
                pipeline_mod, "changed_files", return_value=(["src/foo.py"], None)
            ),
            mock.patch.object(
                pipeline_mod, "select_build_driver", return_value=self.driver
            ),
            mock.patch.object(
                pipeline_mod, "evaluate_changes", return_value=_evaluation()
            ),
            mock.patch.object(
                pipeline_mod,
                "uncommitted_changes_gate",
                return_value=pipeline_mod.BranchStateGateDecision(
                    gate="uncommitted_changes", outcome="pass"
                ),
            ),
            mock.patch.object(
                pipeline_mod,
                "unpushed_commits_gate",
                return_value=pipeline_mod.BranchStateGateDecision(
                    gate="unpushed_commits", outcome="block", payload={}
                ),
            ) as mock_unpushed,
            mock.patch.object(pipeline_mod, "pr_rebase_check") as mock_rebase,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                ),
            )
        mock_unpushed.assert_called_once()
        mock_rebase.assert_not_called()

    def test_branch_state_gates_skipped_on_quality_failure(self) -> None:
        """Quality check failure -> branch-state gates are not evaluated."""
        with (
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(
                pipeline_mod, "changed_files", return_value=(["src/foo.py"], None)
            ),
            mock.patch.object(
                pipeline_mod, "select_build_driver", return_value=self.driver
            ),
            mock.patch.object(
                pipeline_mod,
                "evaluate_changes",
                return_value=_evaluation(blocked=True),
            ),
            mock.patch.object(
                pipeline_mod, "uncommitted_changes_gate"
            ) as mock_uncommitted,
            mock.patch.object(pipeline_mod, "unpushed_commits_gate") as mock_unpushed,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            rc = pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                ),
            )
        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        mock_uncommitted.assert_not_called()
        mock_unpushed.assert_not_called()

    def test_branch_state_gates_run_without_build_driver(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No Makefile or Netsukefile skips quality gates in auto mode."""
        with (
            caplog.at_level("INFO", logger=pipeline_mod.__name__),
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(
                pipeline_mod, "changed_files", return_value=(["src/foo.py"], None)
            ),
            mock.patch.object(pipeline_mod, "select_build_driver", return_value=None),
            mock.patch.object(pipeline_mod, "evaluate_changes") as mock_evaluate,
            mock.patch.object(
                pipeline_mod,
                "uncommitted_changes_gate",
                return_value=pipeline_mod.BranchStateGateDecision(
                    gate="uncommitted_changes", outcome="pass"
                ),
            ) as mock_uncommitted,
            mock.patch.object(
                pipeline_mod,
                "unpushed_commits_gate",
                return_value=pipeline_mod.BranchStateGateDecision(
                    gate="unpushed_commits", outcome="pass"
                ),
            ) as mock_unpushed,
            mock.patch.object(
                pipeline_mod, "pr_rebase_check", return_value=0
            ) as mock_rebase,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            rc = pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                ),
            )

        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        mock_evaluate.assert_not_called()
        mock_uncommitted.assert_called_once()
        mock_unpushed.assert_called_once()
        mock_rebase.assert_called_once()
        assert any(
            record.__dict__.get("operation") == "quality_gate_skip"
            and record.__dict__.get("build_driver") == "auto"
            and record.__dict__.get("manifests_missing") is True
            for record in caplog.records
        )

    def test_branch_state_gates_run_when_no_files_changed(self) -> None:
        """Branch-state gates still run when there are no changed files to lint."""
        with (
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(pipeline_mod, "changed_files", return_value=([], None)),
            mock.patch.object(pipeline_mod, "evaluate_changes") as mock_evaluate,
            mock.patch.object(
                pipeline_mod,
                "uncommitted_changes_gate",
                return_value=pipeline_mod.BranchStateGateDecision(
                    gate="uncommitted_changes", outcome="pass"
                ),
            ) as mock_uncommitted,
            mock.patch.object(
                pipeline_mod,
                "unpushed_commits_gate",
                return_value=pipeline_mod.BranchStateGateDecision(
                    gate="unpushed_commits", outcome="pass"
                ),
            ) as mock_unpushed,
            mock.patch.object(
                pipeline_mod, "pr_rebase_check", return_value=0
            ) as mock_rebase,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            rc = pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                ),
            )
        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        mock_evaluate.assert_not_called()
        mock_uncommitted.assert_called_once()
        mock_unpushed.assert_called_once()
        mock_rebase.assert_called_once()


class TestBranchStateGates:
    """Tests for uncommitted and unpushed gate rendering."""

    def _state_with_upstream(
        self, upstream_ref: str, primary_remote: str | None = "origin"
    ) -> state_mod.HookState:
        """Return hook state with the requested upstream facts."""
        return state_mod.HookState(
            git_facts=GitFacts(
                primary_remote=primary_remote,
                upstream_ref=upstream_ref,
                pr_base_local_ref=None,
                local_base_commit=None,
                three_way_merge_is_configured=False,
            )
        )

    def test_uncommitted_changes_gate_blocks(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Dirty working tree emits the uncommitted template."""
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(True, None)
            ),
        ):
            decision = pipeline_mod.uncommitted_changes_gate(
                REPO, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )
            pipeline_mod._emit_branch_gate_decision(decision)
        payload = json.loads(capsys.readouterr().out)
        assert decision.should_block is True
        assert payload["decision"] == "block"
        assert "Please commit outstanding changes" in payload["reason"]

    def test_uncommitted_changes_gate_skips_protected_branch(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Protected local branches are not prompted for direct commits."""
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("main", None)
            ),
            mock.patch.object(pipeline_mod, "has_uncommitted_changes") as uncommitted,
        ):
            blocked = pipeline_mod.uncommitted_changes_gate(
                REPO,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert blocked.should_block is False
        assert blocked.outcome == "skip"
        assert blocked.matched_branch == "main"
        uncommitted.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_unpushed_commits_gate_blocks(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ahead branch emits the unpushed template."""
        state = self._state_with_upstream("origin/feature")
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "remote_names", return_value=(["origin"], None)
            ),
            mock.patch.object(
                pipeline_mod, "has_unpushed_commits", return_value=(True, None)
            ),
        ):
            decision = pipeline_mod.unpushed_commits_gate(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )
            pipeline_mod._emit_branch_gate_decision(decision)
        payload = json.loads(capsys.readouterr().out)
        assert decision.should_block is True
        assert payload["decision"] == "block"
        assert "origin/feature" in payload["reason"]

    def test_unpushed_commits_gate_skips_protected_branch(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Protected local branches are not prompted for direct pushes."""
        state = self._state_with_upstream("origin/main")
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("main", None)
            ),
            mock.patch.object(pipeline_mod, "has_unpushed_commits") as unpushed,
        ):
            blocked = pipeline_mod.unpushed_commits_gate(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert blocked.should_block is False
        assert blocked.outcome == "skip"
        assert blocked.matched_branch == "main"
        assert blocked.match_kind == "local"
        unpushed.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_unpushed_commits_gate_skips_protected_local_with_unprotected_upstream(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Protected local branches skip even when the upstream is unprotected."""
        state = self._state_with_upstream("origin/feature")
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("main", None)
            ),
            mock.patch.object(pipeline_mod, "has_unpushed_commits") as unpushed,
        ):
            decision = pipeline_mod.unpushed_commits_gate(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert decision.should_block is False
        assert decision.outcome == "skip"
        assert decision.matched_branch == "main"
        assert decision.match_kind == "local"
        unpushed.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_unpushed_commits_gate_skips_protected_tracked_branch(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Protected upstream branch names are not prompted for direct pushes."""
        state = self._state_with_upstream("origin/main")
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "remote_names", return_value=(["origin"], None)
            ),
            mock.patch.object(pipeline_mod, "has_unpushed_commits") as unpushed,
        ):
            blocked = pipeline_mod.unpushed_commits_gate(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert blocked.should_block is False
        assert blocked.outcome == "skip"
        assert blocked.matched_branch == "main"
        assert blocked.match_kind == "upstream"
        unpushed.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_tracked_branch_name_prefers_longest_remote_prefix(self) -> None:
        """Overlapping remote names resolve longest-first, not shortest.

        Kills the ``_tracked_branch_name`` ordering survivor tracked in #34.
        """
        # "up" is a prefix of "up/stream"; stripping "up" first would leave
        # "stream/main". Longest-first ordering must strip "up/stream".
        branch = pipeline_mod._tracked_branch_name(
            "up/stream/main", None, ("up", "up/stream")
        )

        assert branch == "main"

    def test_tracked_branch_name_falls_back_to_first_segment(self) -> None:
        """Unknown remotes strip only the first path segment.

        Kills the ``_tracked_branch_name`` fallback survivor tracked in #34.
        """
        branch = pipeline_mod._tracked_branch_name("weird/feature/x", None, ())

        assert branch == "feature/x"

    def test_candidate_remote_prefixes_orders_longest_first(self) -> None:
        """Prefixes are ordered longest-first and drop empty names.

        Kills the ``_candidate_remote_prefixes`` ordering survivor tracked
        in #34.
        """
        prefixes = pipeline_mod._candidate_remote_prefixes(
            "origin", ("", "up", "up/stream")
        )

        assert prefixes == ("up/stream", "origin", "up")

    def test_unpushed_commits_gate_strips_slash_remote_for_tracked_branch(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Slash-containing remotes do not hide protected upstream branches."""
        state = self._state_with_upstream("team/fork/main", primary_remote="team/fork")
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "remote_names", return_value=(["team/fork"], None)
            ),
            mock.patch.object(pipeline_mod, "has_unpushed_commits") as unpushed,
        ):
            blocked = pipeline_mod.unpushed_commits_gate(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert blocked.should_block is False
        unpushed.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_unpushed_commits_gate_strips_non_primary_slash_remote(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Non-primary slash remotes do not hide protected upstream branches."""
        state = self._state_with_upstream("team/fork/main", primary_remote="origin")
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod,
                "remote_names",
                return_value=(["origin", "team/fork"], None),
            ),
            mock.patch.object(pipeline_mod, "has_unpushed_commits") as unpushed,
        ):
            decision = pipeline_mod.unpushed_commits_gate(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert decision.should_block is False
        assert decision.outcome == "skip"
        assert decision.matched_branch == "main"
        assert decision.match_kind == "upstream"
        unpushed.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_unpushed_commits_gate_blocks_unprotected_branch(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unprotected local branches keep the existing unpushed prompt."""
        state = self._state_with_upstream("origin/feature")
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "remote_names", return_value=(["origin"], None)
            ),
            mock.patch.object(
                pipeline_mod, "has_unpushed_commits", return_value=(True, None)
            ),
        ):
            blocked = pipeline_mod.unpushed_commits_gate(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        pipeline_mod._emit_branch_gate_decision(blocked)
        payload = json.loads(capsys.readouterr().out)
        assert blocked.should_block is True
        assert payload["decision"] == "block"
        assert "origin/feature" in payload["reason"]

    def test_protected_branch_skips_are_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Protected branch skips record bounded branch-state telemetry."""
        state = self._state_with_upstream("origin/main")
        with (
            caplog.at_level("INFO", logger=pipeline_mod.__name__),
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "remote_names", return_value=(["origin"], None)
            ),
            mock.patch.object(pipeline_mod, "has_unpushed_commits") as unpushed,
        ):
            decision = pipeline_mod.unpushed_commits_gate(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert decision.outcome == "skip"
        unpushed.assert_not_called()
        assert any(
            record.__dict__.get("gate") == "unpushed_commits"
            and record.__dict__.get("outcome") == "skip"
            and record.__dict__.get("matched_branch") == "main"
            and record.__dict__.get("match_kind") == "upstream"
            for record in caplog.records
        )

    @given(PROTECTED_BRANCH_SET_STRATEGY)
    def test_unpushed_gate_never_checks_ahead_for_protected_local_branch(
        self, branch_case: tuple[str, tuple[str, ...]]
    ) -> None:
        """Protected local branches never reach the ahead-of-upstream query."""
        protected_branch, protected_branches = branch_case
        state = self._state_with_upstream("origin/feature")
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            config=Config(protected_branches=protected_branches),
        )
        with (
            mock.patch.object(
                pipeline_mod,
                "current_branch",
                return_value=(protected_branch, None),
            ),
            mock.patch.object(pipeline_mod, "has_unpushed_commits") as unpushed,
        ):
            decision = pipeline_mod.unpushed_commits_gate(REPO, state, options)

        assert decision.should_block is False
        assert decision.outcome == "skip"
        assert decision.matched_branch == protected_branch
        assert decision.match_kind == "local"
        unpushed.assert_not_called()

    @given(PROTECTED_BRANCH_SET_STRATEGY, REMOTE_NAME_STRATEGY)
    def test_unpushed_gate_never_checks_ahead_for_protected_upstream_branch(
        self, branch_case: tuple[str, tuple[str, ...]], remote: str
    ) -> None:
        """Protected upstream branches never reach the ahead-of-upstream query."""
        protected_branch, protected_branches = branch_case
        state = self._state_with_upstream(
            f"{remote}/{protected_branch}", primary_remote="origin"
        )
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            config=Config(protected_branches=protected_branches),
        )
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("topic", None)
            ),
            mock.patch.object(
                pipeline_mod, "remote_names", return_value=(["origin", remote], None)
            ),
            mock.patch.object(pipeline_mod, "has_unpushed_commits") as unpushed,
        ):
            decision = pipeline_mod.unpushed_commits_gate(REPO, state, options)

        assert decision.should_block is False
        assert decision.outcome == "skip"
        assert decision.matched_branch == protected_branch
        assert decision.match_kind == "upstream"
        unpushed.assert_not_called()


class TestTargetEnumerationLogging:
    """Target read failures remain visible at the quality execution boundary."""

    def test_enumeration_failure_logs_once(
        self, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A read error is returned once without printing from the unit."""
        error = "could not read /fake/repo/Makefile: permission denied"
        state = state_mod.HookState(changed_files=["app.py"])
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        with mock.patch.object(
            pipeline_mod,
            "get_build_targets",
            return_value=HookError("get_make_targets", error),
        ):
            result = pipeline_mod.evaluate_changes(state, REPO, 12000, driver)

        assert isinstance(result.error, HookError), (
            f"expected HookError but got {result.error!r}"
        )
        assert result.error.operation == "get_make_targets", (
            f"expected get_make_targets but got {result.error.operation!r}"
        )
        assert str(result.error) == error, (
            f"expected {error!r} but got {str(result.error)!r}"
        )
        assert result.blocked is True, f"expected blocked but got {result.blocked!r}"
        assert result.targets_requested == (
            "check-fmt",
            "lint",
            "typecheck",
        ), f"expected requested code targets but got {result.targets_requested!r}"
        assert result.targets_present == (), (
            f"expected no present targets but got {result.targets_present!r}"
        )
        assert result.targets_skipped == (), (
            f"expected no skipped targets but got {result.targets_skipped!r}"
        )
        assert result.commands == (), (
            f"expected no commands but got {result.commands!r}"
        )
        records = [r for r in caplog.records if r.name == pipeline_mod.__name__]
        assert [(r.levelname, r.getMessage()) for r in records] == [
            ("WARNING", f"Could not enumerate build targets: {error}")
        ], "expected exactly one enumeration warning"
        assert capsys.readouterr().out == "", "expected the unit to stay silent"

    def test_disabled_quality_gate_skips_driver_selection(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A disabled quality gate returns quietly without selecting a driver."""
        state = state_mod.HookState(changed_files=["app.py"])
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            config=Config(gate_quality_checks=False),
        )
        with mock.patch.object(pipeline_mod, "select_build_driver") as select:
            result = pipeline_mod.run_quality_checks_if_needed(state, REPO, options)

        assert result is None, f"expected no blocking result but got {result!r}"
        select.assert_not_called()
        assert capsys.readouterr().out == "", "expected no output"

    def test_orchestrator_emits_enumeration_failure(
        self, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The orchestrator owns the single warning and blocking JSON payload."""
        error = "could not read /fake/repo/Makefile: permission denied"
        state = state_mod.HookState(changed_files=["app.py"])
        options = state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        with (
            mock.patch.object(pipeline_mod, "select_build_driver", return_value=driver),
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value=HookError("get_make_targets", error),
            ),
        ):
            result = pipeline_mod.run_quality_checks_if_needed(state, REPO, options)

        assert result == 0, f"expected hook exit code 0 but got {result!r}"
        records = [r for r in caplog.records if r.name == pipeline_mod.__name__]
        assert [(r.levelname, r.getMessage()) for r in records] == [
            ("WARNING", f"Could not enumerate build targets: {error}")
        ], "expected the orchestrator to log the warning once"
        payload = json.loads(capsys.readouterr().out)
        assert payload["decision"] == "block", (
            f"expected a block decision but got {payload!r}"
        )
        assert error in payload["reason"], (
            f"expected {error!r} in the reason but got {payload['reason']!r}"
        )


class TestPrRebaseCheck:
    """Tests for the pull-request rebase gate."""

    @pytest.mark.parametrize("target_error", [None, "Makefile permission denied"])
    def test_pr_base_ahead_blocks(
        self,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
        target_error: str | None,
    ) -> None:
        """Ahead PR base emits the rebase template as a block payload."""
        state = state_mod.HookState(
            git_facts=GitFacts(
                primary_remote="origin",
                upstream_ref="origin/feature",
                pr_base_local_ref=None,
                local_base_commit="base",
                three_way_merge_is_configured=True,
            )
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
            mock.patch.object(pipeline_mod, "_pr_base_is_ahead", return_value=True),
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value=(
                    HookError("get_make_targets", target_error)
                    if target_error
                    else {"check-fmt", "lint", "typecheck"}
                ),
            ),
        ):
            rc = pipeline_mod.pr_rebase_check(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["decision"] == "block"
        assert "Please rebase this branch onto `origin/main`" in payload["reason"]
        assert "using the `rebase` skill" in payload["reason"]
        assert ("`make typecheck`" in payload["reason"]) is (target_error is None)
        records = [r for r in caplog.records if r.name == pipeline_mod.__name__]
        expected = (
            [("WARNING", f"Could not enumerate build targets: {target_error}")]
            if target_error
            else []
        )
        assert [(r.levelname, r.getMessage()) for r in records] == expected

    def test_pr_lookup_skipped_without_primary_remote(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Missing primary remote silently skips the PR gate."""
        state = state_mod.HookState(
            git_facts=GitFacts(
                primary_remote=None,
                upstream_ref=None,
                pr_base_local_ref=None,
                local_base_commit=None,
                three_way_merge_is_configured=False,
            )
        )
        with mock.patch.object(pipeline_mod, "lookup_pr") as mock_lookup:
            rc = pipeline_mod.pr_rebase_check(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )
        assert rc == 0
        mock_lookup.assert_not_called()
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# run() - OSError resilience

# ---------------------------------------------------------------------------


class TestHookStateImmutability:
    """Tests for the frozen reporting snapshot."""

    def test_attribute_assignment_is_rejected(self) -> None:
        """Check HookState cannot be mutated through attribute assignment."""
        state = state_mod.HookState()
        # The write is deliberately invalid; the frozen dataclass must reject it.
        with pytest.raises(dataclasses.FrozenInstanceError):
            state.ok = False  # ty: ignore[invalid-assignment]


# ---------------------------------------------------------------------------
# format_reason

# ---------------------------------------------------------------------------


class TestFormatReason:
    """Tests for format_reason()."""

    def test_returns_non_empty_string(self) -> None:
        """Check format_reason returns a non-empty string for a default state."""
        state = state_mod.HookState()
        reason = formatting_mod.format_reason(state)
        assert isinstance(reason, str), f"expected str but got {type(reason)}"
        assert len(reason) > 0, "expected non-empty reason string"

    def test_contains_base_ref(self) -> None:
        """Check the formatted reason mentions the base ref."""
        state = state_mod.HookState(base_ref="origin/main")
        reason = formatting_mod.format_reason(state)
        assert "origin/main" in reason, (
            f"expected base_ref in reason but got {reason!r}"
        )

    def test_contains_error_message(self) -> None:
        """Check the formatted reason includes the recorded error message."""
        state = state_mod.HookState(error="something broke")
        reason = formatting_mod.format_reason(state)
        assert "something broke" in reason, (
            f"expected error message in reason but got {reason!r}"
        )


# ---------------------------------------------------------------------------
# block_and_print

# ---------------------------------------------------------------------------


class TestBlockAndPrint:
    """Tests for block_and_print()."""

    def test_writes_valid_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Check block_and_print writes a JSON object to stdout."""
        state = state_mod.HookState()
        formatting_mod.block_and_print(state)
        out = json.loads(capsys.readouterr().out)
        assert isinstance(out, dict), f"expected JSON object but got {type(out)}"

    def test_json_has_decision_and_reason(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Check the printed JSON has both decision and reason keys."""
        state = state_mod.HookState()
        formatting_mod.block_and_print(state)
        out = json.loads(capsys.readouterr().out)
        assert "decision" in out, f"expected 'decision' key in {out}"
        assert "reason" in out, f"expected 'reason' key in {out}"

    def test_decision_is_block(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Check the printed decision is block."""
        state = state_mod.HookState()
        formatting_mod.block_and_print(state)
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block", (
            f"expected block decision but got {out['decision']!r}"
        )

    def test_returns_zero(self) -> None:
        """Check block_and_print returns 0."""
        state = state_mod.HookState()
        rc = formatting_mod.block_and_print(state)
        assert rc == 0, f"expected return 0 but got {rc!r}"


# ---------------------------------------------------------------------------
# targets_for_categories

# ---------------------------------------------------------------------------


class TestQualityUnits:
    """Tests for the split category, target and execution units."""

    driver = driver_mod.BuildDriver("make", "make", "Makefile")

    def test_select_categories_maps_files_to_requested_targets(self) -> None:
        """Detected categories expand into their declared target lists."""
        selection = pipeline_mod.select_categories([
            "src/app.py",
            "docs/users-guide.md",
        ])

        assert selection.categories == {"code", "markdown"}, (
            f"expected code and markdown but got {selection.categories!r}"
        )
        assert selection.targets_requested == [
            "check-fmt",
            "lint",
            "typecheck",
            "markdownlint",
            "nixie",
        ], f"expected both target groups but got {selection.targets_requested!r}"

    def test_select_categories_ignores_unrecognized_files(self) -> None:
        """Files without a recognized extension request no targets."""
        selection = pipeline_mod.select_categories(["LICENSE"])

        assert selection.categories == set(), (
            f"expected no categories but got {selection.categories!r}"
        )
        assert selection.targets_requested == [], (
            f"expected no requested targets but got {selection.targets_requested!r}"
        )

    def test_enumerate_quality_targets_separates_present_and_missing(self) -> None:
        """Requested targets are split into present, skipped and grouped lists."""
        selection = state_mod.CategorySelection(
            categories={"code", "markdown"},
            targets_requested=[
                "check-fmt",
                "lint",
                "typecheck",
                "markdownlint",
                "nixie",
            ],
        )
        with mock.patch.object(
            pipeline_mod,
            "get_build_targets",
            return_value={"check-fmt", "markdownlint"},
        ):
            targets = pipeline_mod.enumerate_quality_targets(
                REPO, self.driver, selection
            )

        assert targets == state_mod.TargetSelection(
            targets_requested=(
                "check-fmt",
                "lint",
                "typecheck",
                "markdownlint",
                "nixie",
            ),
            targets_present=("check-fmt", "markdownlint"),
            targets_skipped=("lint", "typecheck", "nixie"),
            code_targets=("check-fmt",),
            markdown_targets=("markdownlint",),
        ), f"unexpected target selection {targets!r}"

    def test_enumerate_quality_targets_skips_all_missing_targets(self) -> None:
        """A driver with no matching targets succeeds with everything skipped."""
        selection = state_mod.CategorySelection(
            categories={"code"},
            targets_requested=["check-fmt", "lint", "typecheck"],
        )
        with mock.patch.object(
            pipeline_mod, "get_build_targets", return_value={"build"}
        ):
            targets = pipeline_mod.enumerate_quality_targets(
                REPO, self.driver, selection
            )

        assert isinstance(targets, state_mod.TargetSelection), (
            f"expected a target selection but got {targets!r}"
        )
        assert targets.targets_present == (), (
            f"expected no present targets but got {targets.targets_present!r}"
        )
        assert targets.targets_skipped == ("check-fmt", "lint", "typecheck"), (
            f"expected every requested target to be skipped but got "
            f"{targets.targets_skipped!r}"
        )
        assert targets.code_targets == (), (
            f"expected no code targets but got {targets.code_targets!r}"
        )

    def test_enumerate_quality_targets_returns_the_enumeration_error(self) -> None:
        """A driver read failure is returned unchanged."""
        error = HookError("get_make_targets", "permission denied")
        selection = state_mod.CategorySelection(
            categories={"code"},
            targets_requested=["check-fmt"],
        )
        with mock.patch.object(pipeline_mod, "get_build_targets", return_value=error):
            targets = pipeline_mod.enumerate_quality_targets(
                REPO, self.driver, selection
            )

        assert targets is error, (
            f"expected the enumeration error to be returned unchanged but got "
            f"{targets!r}"
        )

    def test_execute_quality_targets_runs_code_then_markdown_after_failure(
        self,
    ) -> None:
        """A code failure does not prevent the Markdown group from running."""
        code_result = _command(kind="code", exit_code=1)
        markdown_result = _command(kind="markdown")
        selection = state_mod.TargetSelection(
            code_targets=("check-fmt",),
            markdown_targets=("markdownlint",),
        )
        with mock.patch.object(
            pipeline_mod,
            "run_build_targets",
            side_effect=[code_result, markdown_result],
        ) as run_targets:
            commands = pipeline_mod.execute_quality_targets(
                REPO, self.driver, selection, 12000
            )

        assert commands == (code_result, markdown_result), (
            f"expected both command results in order but got {commands!r}"
        )
        kinds = [call.args[1].kind for call in run_targets.call_args_list]
        assert kinds == ["code", "markdown"], (
            f"expected code then markdown groups but got {kinds!r}"
        )

    def test_execute_quality_targets_skips_empty_groups(self) -> None:
        """Groups with no present targets do not invoke the build driver."""
        with mock.patch.object(pipeline_mod, "run_build_targets") as run_targets:
            commands = pipeline_mod.execute_quality_targets(
                REPO,
                self.driver,
                state_mod.TargetSelection(),
                12000,
            )

        assert commands == (), f"expected no command results but got {commands!r}"
        run_targets.assert_not_called()


class TestPrepareRunStopChecks:
    """Tests for immutable preparation outcomes."""

    def test_non_repository_returns_stop(self, tmp_path: Path) -> None:
        """A missing working directory is a quiet non-repository stop."""
        start_cwd = tmp_path / "missing"
        with (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(
                pipeline_mod,
                "repo_root",
                return_value=(None, "not a git repository"),
            ),
        ):
            preparation = pipeline_mod.prepare_run_stop_checks(
                start_cwd,
                "origin/main",
                always_fetch=False,
                config=Config(),
            )

        assert preparation.outcome == "stop", (
            f"expected a quiet stop but got {preparation.outcome!r}"
        )
        assert preparation.repo is None, (
            f"expected no repository root but got {preparation.repo!r}"
        )
        assert preparation.error is None, (
            f"expected no preparation error but got {preparation.error!r}"
        )
        assert preparation.state.error is None, (
            f"expected no reported error but got {preparation.state.error!r}"
        )

    def test_missing_git_blocks_with_a_hook_error(self) -> None:
        """A missing Git executable is reported as a blocking hook error."""
        with mock.patch.object(
            pipeline_mod, "_is_executable_available", return_value=False
        ):
            preparation = pipeline_mod.prepare_run_stop_checks(
                REPO,
                "origin/main",
                always_fetch=False,
                config=Config(),
            )

        assert preparation.outcome == "block", (
            f"expected a block outcome but got {preparation.outcome!r}"
        )
        assert isinstance(preparation.error, HookError), (
            f"expected a HookError but got {preparation.error!r}"
        )
        assert preparation.error.operation == "prepare_run_stop_checks", (
            f"expected the prepare operation but got {preparation.error.operation!r}"
        )
        assert str(preparation.error) == "git not found on PATH", (
            f"expected the missing-git message but got {str(preparation.error)!r}"
        )
        assert preparation.state.ok is False, (
            f"expected an unsuccessful snapshot but got {preparation.state.ok!r}"
        )
        assert preparation.state.error == "git not found on PATH", (
            f"expected the missing-git message in the snapshot but got "
            f"{preparation.state.error!r}"
        )

    def test_changed_files_failure_keeps_merge_base_reporting(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A changed-file failure retains prepared facts in the block report."""
        options = state_mod.StopCheckOptions(always_fetch=True, max_out=12000)
        patches = (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "collect_git_facts", return_value=GIT_FACTS
            ),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, True)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(
                pipeline_mod,
                "changed_files",
                return_value=(None, "git diff failed"),
            ),
        )
        with _enter_patchers(patches):
            preparation = pipeline_mod.prepare_run_stop_checks(
                REPO,
                "origin/main",
                always_fetch=options.always_fetch,
                config=options.config,
            )

        assert preparation.outcome == "block", (
            f"expected a block outcome but got {preparation.outcome!r}"
        )
        assert isinstance(preparation.error, HookError), (
            f"expected a HookError but got {preparation.error!r}"
        )
        assert preparation.error.operation == "changed_files", (
            f"expected the changed_files operation but got "
            f"{preparation.error.operation!r}"
        )
        assert str(preparation.error) == "git diff failed", (
            f"expected the diff failure message but got {str(preparation.error)!r}"
        )
        assert preparation.state.base_commit == "abc123", (
            f"expected the merge base to be retained but got "
            f"{preparation.state.base_commit!r}"
        )
        assert preparation.state.fetched is True, (
            f"expected the fetch fact to be retained but got "
            f"{preparation.state.fetched!r}"
        )
        assert preparation.state.ok is False, (
            f"expected an unsuccessful snapshot but got {preparation.state.ok!r}"
        )

        with _enter_patchers(patches):
            rc = pipeline_mod.run_stop_checks(REPO, "origin/main", options)

        payload = json.loads(capsys.readouterr().out)
        assert rc == 0, f"expected return 0 but got {rc!r}"
        assert payload["decision"] == "block", (
            f"expected a blocking decision but got {payload['decision']!r}"
        )
        assert "Diff base: origin/main (abc123)" in payload["reason"], (
            f"expected the retained merge base in the reason but got "
            f"{payload['reason']!r}"
        )
        assert "Error: git diff failed" in payload["reason"], (
            f"expected the diff failure in the reason but got {payload['reason']!r}"
        )


@dataclasses.dataclass(slots=True)
class QualityRun:
    """Captured boundaries from one quality-stage run."""

    rc: int
    run_targets: mock.Mock
    uncommitted: mock.Mock
    unpushed: mock.Mock
    rebase: mock.Mock


def _run_stop_checks_with_quality(
    targets: set[str] | HookError,
    run_results: list[exec_mod.CommandResult],
    *,
    changed_files: list[str] | None = None,
) -> QualityRun:
    """Run stop checks with real quality units and patched boundaries."""
    changed = (
        ["src/app.py", "docs/users-guide.md"]
        if changed_files is None
        else changed_files
    )
    driver = driver_mod.BuildDriver("make", "make", "Makefile")
    passing = pipeline_mod.BranchStateGateDecision(gate="test", outcome="pass")
    with (
        mock.patch.object(pipeline_mod, "_is_executable_available", return_value=True),
        mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
        mock.patch.object(pipeline_mod, "collect_git_facts", return_value=GIT_FACTS),
        mock.patch.object(
            pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
        ),
        mock.patch.object(pipeline_mod, "merge_base", return_value=("abc123", None)),
        mock.patch.object(pipeline_mod, "changed_files", return_value=(changed, None)),
        mock.patch.object(pipeline_mod, "select_build_driver", return_value=driver),
        mock.patch.object(pipeline_mod, "get_build_targets", return_value=targets),
        mock.patch.object(
            pipeline_mod, "run_build_targets", side_effect=run_results
        ) as run_targets,
        mock.patch.object(
            pipeline_mod,
            "uncommitted_changes_gate",
            return_value=passing,
        ) as uncommitted,
        mock.patch.object(
            pipeline_mod,
            "unpushed_commits_gate",
            return_value=passing,
        ) as unpushed,
        mock.patch.object(pipeline_mod, "pr_rebase_check", return_value=0) as rebase,
    ):
        rc = pipeline_mod.run_stop_checks(
            REPO,
            "origin/main",
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
        )
    return QualityRun(
        rc=rc,
        run_targets=run_targets,
        uncommitted=uncommitted,
        unpushed=unpushed,
        rebase=rebase,
    )


class TestRunStopChecksQualityIntegration:
    """Integration tests for run_stop_checks with the real quality units."""

    def test_failed_code_build_blocks_before_branch_gates(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Mixed changes run code then Markdown and block on the code failure."""
        targets = {
            "check-fmt",
            "lint",
            "typecheck",
            "markdownlint",
            "nixie",
        }
        result = _run_stop_checks_with_quality(
            targets,
            [
                _command(
                    kind="code",
                    exit_code=1,
                    cmd="make --no-print-directory check-fmt lint typecheck",
                ),
                _command(
                    kind="markdown",
                    cmd="make --no-print-directory markdownlint nixie",
                ),
            ],
        )

        assert result.rc == 0, f"expected return 0 but got {result.rc!r}"
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        assert result.run_targets.call_args_list[0].args == (
            REPO,
            exec_mod.BuildTargetRequest(
                driver, "code", ["check-fmt", "lint", "typecheck"]
            ),
            12000,
        ), (
            "expected the code group to be run first but got "
            f"{result.run_targets.call_args_list[0].args!r}"
        )
        assert result.run_targets.call_args_list[1].args == (
            REPO,
            exec_mod.BuildTargetRequest(driver, "markdown", ["markdownlint", "nixie"]),
            12000,
        ), (
            "expected the Markdown group to be run second but got "
            f"{result.run_targets.call_args_list[1].args!r}"
        )
        payload = json.loads(capsys.readouterr().out)
        reason = payload["reason"]
        assert payload["decision"] == "block", (
            f"expected a blocking decision but got {payload['decision']!r}"
        )
        assert "Detected change types: Code, Markdown" in reason, (
            f"expected both detected categories in the reason but got {reason!r}"
        )
        assert (
            "Targets present: check-fmt lint typecheck markdownlint nixie" in reason
        ), f"expected every present target in the reason but got {reason!r}"
        assert (
            "Command failed (exit 1): "
            "make --no-print-directory check-fmt lint typecheck"
        ) in reason, f"expected the code failure in the reason but got {reason!r}"
        assert "markdownlint nixie" not in reason.split("Command failed")[-1], (
            f"expected the passing Markdown group to be omitted but got {reason!r}"
        )
        result.uncommitted.assert_not_called()
        result.unpushed.assert_not_called()
        result.rebase.assert_not_called()

    def test_passing_builds_stay_silent_and_run_branch_gates(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Passing quality checks continue to the branch-state gates."""
        result = _run_stop_checks_with_quality(
            {"check-fmt", "lint", "typecheck", "markdownlint", "nixie"},
            [_command(kind="code"), _command(kind="markdown")],
        )

        assert result.rc == 0, f"expected return 0 but got {result.rc!r}"
        assert capsys.readouterr().out == "", "expected no stdout on a passing run"
        result.uncommitted.assert_called_once()
        result.unpushed.assert_called_once()
        result.rebase.assert_called_once()

    def test_missing_targets_skip_without_printing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A driver with none of the requested targets stays silent."""
        result = _run_stop_checks_with_quality({"build"}, [])

        assert result.rc == 0, f"expected return 0 but got {result.rc!r}"
        result.run_targets.assert_not_called()
        assert capsys.readouterr().out == "", (
            "expected no stdout when every target is missing"
        )
        result.uncommitted.assert_called_once()

    def test_enumeration_failure_blocks_with_one_warning(
        self, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An enumeration failure emits one warning and one block payload."""
        error = HookError("get_make_targets", "permission denied")
        result = _run_stop_checks_with_quality(error, [])

        assert result.rc == 0, f"expected return 0 but got {result.rc!r}"
        records = [r for r in caplog.records if r.name == pipeline_mod.__name__]
        assert [(r.levelname, r.getMessage()) for r in records] == [
            ("WARNING", "Could not enumerate build targets: permission denied")
        ], f"expected exactly one warning record but got {records!r}"
        payload = json.loads(capsys.readouterr().out)
        assert payload["decision"] == "block", (
            f"expected a blocking decision but got {payload['decision']!r}"
        )
        assert "permission denied" in payload["reason"], (
            f"expected the enumeration failure in the reason but got "
            f"{payload['reason']!r}"
        )
        result.uncommitted.assert_not_called()
        result.unpushed.assert_not_called()
        result.rebase.assert_not_called()
