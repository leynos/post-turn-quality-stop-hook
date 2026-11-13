"""Cover stop-check orchestration, branch-state and PR-rebase gates.

The tests also cover error handling and block-output formatting. The legacy
``compush_check`` helper is tested separately as a downstream-compatibility
API, not as an active gate.
"""

from __future__ import annotations

import json
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import time
import typing as typ
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from post_turn_quality_stop_hook import decision_log as log_mod
from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import formatting as formatting_mod
from post_turn_quality_stop_hook import github as github_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.decision_log import DecisionLogSettings
from post_turn_quality_stop_hook.git import ExcludedPath
from post_turn_quality_stop_hook.git_facts import GitFacts
from post_turn_quality_stop_hook.recording import InvocationLog

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook.git import FetchObserver

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

# A run started this far in the past must report at least this much startup
# time; the margin keeps the assertion stable on a loaded machine.
STARTUP_STAGE_MIN_MS = 200


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
                pipeline_mod, "select_build_driver", return_value=(self.driver, None)
            ),
            mock.patch.object(pipeline_mod, "evaluate_changes", return_value=0),
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
                pipeline_mod, "select_build_driver", return_value=(self.driver, None)
            ),
            mock.patch.object(pipeline_mod, "evaluate_changes", return_value=0),
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
                pipeline_mod, "select_build_driver", return_value=(self.driver, None)
            ),
            mock.patch.object(pipeline_mod, "evaluate_changes", return_value=1),
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
            mock.patch.object(
                pipeline_mod, "select_build_driver", return_value=(None, None)
            ),
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

    @pytest.mark.parametrize(
        ("dirty", "outcome"),
        [(True, "block"), (False, "pass")],
        ids=["dirty", "clean"],
    )
    def test_uncommitted_gate_matches_when_exclusion_resolution_fails(
        self,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
        dirty: bool,
        outcome: str,
    ) -> None:
        """A failed exclusion lookup leaves the decision and stdout unchanged."""
        warning = "could not resolve the decision-log exclusion"

        def run_gate(
            result: tuple[bool, str | None],
            exclude: tuple[ExcludedPath, ...],
        ) -> tuple[pipeline_mod.BranchStateGateDecision, str]:
            """Run the gate with the adapter result and exclusion supplied."""
            with (
                mock.patch.object(
                    pipeline_mod, "current_branch", return_value=("feature", None)
                ),
                mock.patch.object(
                    pipeline_mod, "has_uncommitted_changes", return_value=result
                ),
            ):
                decision = pipeline_mod.uncommitted_changes_gate(
                    REPO,
                    state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                    exclude=exclude,
                )
                pipeline_mod._emit_branch_gate_decision(decision)
            return decision, capsys.readouterr().out

        fallback, fallback_stdout = run_gate(
            (dirty, warning), (ExcludedPath(Path("/state")),)
        )
        fallback_warnings = caplog.text
        logging_off, logging_off_stdout = run_gate((dirty, None), ())

        assert fallback.outcome == logging_off.outcome == outcome
        assert fallback == logging_off, (
            "a failed exclusion lookup must not change the gate decision"
        )
        assert fallback_stdout == logging_off_stdout, (
            "a failed exclusion lookup must not change the emitted payload"
        )
        assert "Could not resolve the decision-log exclusion" in fallback_warnings

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

    def test_uncommitted_gate_keeps_git_output_out_of_the_reason(self) -> None:
        """A Git failure records a fixed reason and keeps the detail."""
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod,
                "has_uncommitted_changes",
                return_value=(None, "fatal: SECRET OUTPUT"),
            ),
        ):
            decision = pipeline_mod.uncommitted_changes_gate(
                REPO, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )

        assert decision.outcome == "pass"
        assert decision.reason == "git error"
        assert decision.error == "fatal: SECRET OUTPUT"

    def test_uncommitted_gate_protection_failure_keeps_output_out_of_the_reason(
        self,
    ) -> None:
        """A branch-protection lookup failure records a fixed reason."""
        protection = pipeline_mod.BranchProtectionCheck(
            is_protected=False, error="fatal: SECRET OUTPUT"
        )
        with mock.patch.object(
            pipeline_mod, "_current_branch_protection", return_value=protection
        ):
            decision = pipeline_mod.uncommitted_changes_gate(
                REPO, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )

        assert decision.reason == "git error"
        assert decision.error == "fatal: SECRET OUTPUT"

    def test_unpushed_gate_keeps_git_output_out_of_the_reason(self) -> None:
        """An ahead-of-upstream failure records a fixed reason."""
        state = self._state_with_upstream("origin/feature")
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "remote_names", return_value=(["origin"], None)
            ),
            mock.patch.object(
                pipeline_mod,
                "has_unpushed_commits",
                return_value=(None, "fatal: SECRET OUTPUT"),
            ),
        ):
            decision = pipeline_mod.unpushed_commits_gate(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert decision.outcome == "pass"
        assert decision.reason == "git error"
        assert decision.error == "fatal: SECRET OUTPUT"

    def test_protected_branch_lookup_failure_keeps_output_out_of_the_reason(
        self,
    ) -> None:
        """A tracked-branch lookup failure records a fixed reason."""
        protection = pipeline_mod.BranchProtectionCheck(
            is_protected=False, error="fatal: SECRET OUTPUT"
        )

        decision = pipeline_mod._protected_branch_skip_decision(
            "unpushed_commits", protection, "local"
        )

        assert decision is not None
        assert decision.reason == "git error"
        assert decision.error == "fatal: SECRET OUTPUT"


class TestTargetEnumerationLogging:
    """Target read failures remain visible at the quality execution boundary."""

    def test_enumeration_failure_logs_once(
        self, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A read error produces one warning and a structured blocking reason."""
        error = "could not read /fake/repo/Makefile: permission denied"
        state = state_mod.HookState(changed_files=["app.py"])
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        with mock.patch.object(
            pipeline_mod, "get_build_targets", return_value=(None, error, 1)
        ):
            result = pipeline_mod.evaluate_changes(state, REPO, 12000, driver)

        assert result == pipeline_mod.BLOCKED_STATUS
        records = [r for r in caplog.records if r.name == pipeline_mod.__name__]
        assert [(r.levelname, r.getMessage()) for r in records] == [
            ("WARNING", f"Could not enumerate build targets: {error}")
        ]
        payload = json.loads(capsys.readouterr().out)
        assert payload["decision"] == "block"
        assert error in payload["reason"]


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
            mock.patch.object(
                pipeline_mod,
                "_pr_base_is_ahead",
                return_value=(True, "pull request base is ahead"),
            ),
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value=(
                    None if target_error else {"check-fmt", "lint", "typecheck"},
                    target_error,
                    1 if target_error else 0,
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


class TestFailState:
    """Tests for fail_state()."""

    def test_sets_ok_false_and_error(self) -> None:
        """Check fail_state clears ok and records the error message."""
        state = state_mod.HookState()
        rc = formatting_mod.fail_state(state, "something went wrong")
        assert state.ok is False, f"expected ok=False but was {state.ok!r}"
        assert state.error == "something went wrong", (
            f"expected error message but got {state.error!r}"
        )
        assert rc == 0, f"expected return 0 but got {rc!r}"

    def test_none_message(self) -> None:
        """Check fail_state accepts a None message and leaves the error unset."""
        state = state_mod.HookState()
        formatting_mod.fail_state(state, None)
        assert state.ok is False
        assert state.error is None, f"expected error=None but got {state.error!r}"

    def test_log_reason_is_recorded_separately(self) -> None:
        """The fixed log reason never replaces the user-facing error."""
        state = state_mod.HookState()

        formatting_mod.fail_state(
            state, "SECRET OUTPUT", log_reason="could not resolve repository root"
        )

        assert state.error == "SECRET OUTPUT"
        assert state.log_reason == "could not resolve repository root"

    def test_error_without_a_log_reason_is_not_logged(self) -> None:
        """A raw error never becomes the logged reason by default."""
        state = state_mod.HookState()

        formatting_mod.fail_state(state, "fatal: SECRET OUTPUT")

        assert formatting_mod._decision_reason(state) == "blocking condition detected"


class TestRecordRepositoryFacts:
    """Log-only Git facts never abort preparation or change the decision."""

    def test_git_start_failure_is_ignored(self) -> None:
        """A Git process that cannot start leaves the repository unrecorded."""
        log = mock.Mock()
        with mock.patch.object(
            pipeline_mod,
            "current_branch",
            side_effect=OSError("cannot start git"),
        ):
            pipeline_mod._record_repository_facts(log, REPO, "origin/main", None)

        log.set_repository.assert_not_called()


class TestRecordQuality:
    """Quality selection facts reach the decision log unchanged."""

    def test_selection_reason_is_recorded(self) -> None:
        """The log explains why the build driver was selected."""
        log = mock.Mock()
        state = state_mod.HookState(
            build_driver="make",
            build_driver_reason="explicitly requested: make",
            targets_requested=["lint"],
            targets_available=["lint"],
            targets_run=["lint"],
        )

        pipeline_mod._record_quality(log, state)

        log.set_quality.assert_called_once_with(
            build_driver="make",
            build_driver_reason="explicitly requested: make",
            targets_requested=["lint"],
            targets_available=["lint"],
            targets_run=["lint"],
            targets_skipped=[],
        )


class TestBuildCommandLogging:
    """Logged build commands name the driver, not its configured path."""

    def test_environment_derived_executable_is_not_logged(self) -> None:
        """A custom executable path from the environment stays out of the log."""
        log = mock.Mock()
        state = state_mod.HookState(decision_log=log)
        driver = driver_mod.BuildDriver(
            "make", "/home/user/bin/make-SECRET", "Makefile"
        )
        request = pipeline_mod.BuildTargetRequest(driver, "code", ["lint"])
        result = {
            "kind": "code",
            "cmd": "",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }

        with mock.patch.object(pipeline_mod, "run_build_targets", return_value=result):
            pipeline_mod._run_build_group(state, REPO, request, 12000)

        logged = log.command_start.call_args.args[1]
        assert logged == "make --no-print-directory lint"
        assert "SECRET" not in logged


class TestPrepareLogReasons:
    """Preparation failures keep Git output out of the decision log."""

    def test_repo_root_failure_uses_a_fixed_log_reason(self, tmp_path: Path) -> None:
        """A repository-root lookup failure logs a fixed reason."""
        with (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(
                pipeline_mod, "repo_root", return_value=(None, "fatal: SECRET OUTPUT")
            ),
        ):
            preparation = pipeline_mod.prepare_run_stop_checks(
                tmp_path,
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert preparation.ok is False
        assert preparation.state.error == "fatal: SECRET OUTPUT"
        assert preparation.state.log_reason == "could not resolve repository root"

    def test_base_ref_failure_uses_a_fixed_log_reason(self, tmp_path: Path) -> None:
        """A base-ref fetch failure logs a fixed reason."""
        with (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(pipeline_mod, "repo_root", return_value=(tmp_path, None)),
            mock.patch.object(
                pipeline_mod, "collect_git_facts", return_value=_NO_FACTS
            ),
            mock.patch.object(
                pipeline_mod,
                "ensure_base_ref",
                return_value=(False, "fatal: SECRET OUTPUT", False),
            ),
        ):
            preparation = pipeline_mod.prepare_run_stop_checks(
                tmp_path,
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert preparation.state.error == "fatal: SECRET OUTPUT"
        assert preparation.state.log_reason == "could not ensure base ref"

    def test_merge_base_failure_uses_a_fixed_log_reason(self, tmp_path: Path) -> None:
        """A merge-base lookup failure logs a fixed reason."""
        with (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(pipeline_mod, "repo_root", return_value=(tmp_path, None)),
            mock.patch.object(
                pipeline_mod, "collect_git_facts", return_value=_NO_FACTS
            ),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=(None, "fatal: SECRET OUTPUT")
            ),
        ):
            preparation = pipeline_mod.prepare_run_stop_checks(
                tmp_path,
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert preparation.state.error == "fatal: SECRET OUTPUT"
        assert preparation.state.log_reason == "could not find merge base"

    def test_changed_files_failure_uses_a_fixed_log_reason(
        self, tmp_path: Path
    ) -> None:
        """A changed-files listing failure logs a fixed reason."""
        with (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(pipeline_mod, "repo_root", return_value=(tmp_path, None)),
            mock.patch.object(
                pipeline_mod, "collect_git_facts", return_value=_NO_FACTS
            ),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(
                pipeline_mod,
                "changed_files",
                return_value=(None, "fatal: SECRET OUTPUT"),
            ),
        ):
            preparation = pipeline_mod.prepare_run_stop_checks(
                tmp_path,
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert preparation.state.error == "fatal: SECRET OUTPUT"
        assert preparation.state.log_reason == "could not list changed files"


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
# decision log integration


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


class TestDecisionLogIntegration:
    """The pipeline records gate outcomes without changing hook output."""

    driver = driver_mod.BuildDriver("make", "make", "Makefile")

    def test_allowed_run_records_every_gate_and_reason(self, tmp_path: Path) -> None:
        """A stop is joinable to its transcript and records all four gates."""
        log = _decision_log(tmp_path)
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
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("base123", None)
            ),
            mock.patch.object(pipeline_mod, "changed_files", return_value=([], None)),
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
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
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("base123", None)
            ),
            mock.patch.object(
                pipeline_mod,
                "changed_files",
                return_value=(["src/foo.py"], None),
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
            mock.patch("shutil.which", return_value="/usr/bin/git"),
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
    ) -> None:
        """A netsuke failure keeps its output in the response, not the log."""
        log = _decision_log(tmp_path)
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
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("base123", None)
            ),
            mock.patch.object(
                pipeline_mod,
                "changed_files",
                return_value=(["src/foo.py"], None),
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
            mock.patch("shutil.which", return_value="/usr/bin/git"),
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
    ) -> None:
        """A rejected driver name stays in the response, not the log."""
        log = _decision_log(tmp_path)
        error = "Unsupported build driver 'SECRET-DRIVER'. Use make, netsuke."
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
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("base123", None)
            ),
            mock.patch.object(
                pipeline_mod,
                "changed_files",
                return_value=(["src/foo.py"], None),
            ),
            mock.patch.object(
                pipeline_mod,
                "select_build_driver",
                return_value=(None, error),
            ),
            mock.patch("shutil.which", return_value="/usr/bin/git"),
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
        log.finish()

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
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(pipeline_mod, "ensure_base_ref", side_effect=ensure),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("base123", None)
            ),
            mock.patch.object(pipeline_mod, "changed_files", return_value=([], None)),
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
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ) as dirty_mock,
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("base123", None)
            ),
            mock.patch.object(
                pipeline_mod, "changed_files", return_value=([], None)
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
            rc = pipeline_mod.run_branch_state_gates(
                REPO,
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                exclude_paths=(
                    ExcludedPath(
                        log_mod.log_directory(tmp_path / "state"),
                        "hook-2026-11-06.jsonl",
                    ),
                ),
            )
        log.finish()

        assert rc == 0
        assert uncommitted_mock.call_args.kwargs["exclude"] == (
            ExcludedPath(
                log_mod.log_directory(tmp_path / "state"), "hook-2026-11-06.jsonl"
            ),
        )
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
        log.finish()

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
        assert quality["targets_requested"] == ["check-fmt", "lint", "typecheck"]


class TestExclusionFallback:
    """A log-exclusion fallback is warned about without changing the decision."""

    def test_changed_files_keeps_the_result(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The adapter preserves the exclusion, so the result stands."""
        with mock.patch.object(
            pipeline_mod,
            "changed_files",
            return_value=(["a.py"], "could not resolve the decision-log exclusion"),
        ) as files_mock:
            files, err = pipeline_mod._changed_files_with_exclusion(
                REPO, "abc123", (ExcludedPath(Path("/state")),)
            )

        assert files == ["a.py"], f"expected the adapter result but got {files!r}"
        assert err is None, f"expected no error but got {err!r}"
        files_mock.assert_called_once_with(
            REPO, "abc123", exclude=(ExcludedPath(Path("/state")),)
        )
        assert "Could not resolve the decision-log exclusion" in caplog.text

    def test_dirty_keeps_the_result(self, caplog: pytest.LogCaptureFixture) -> None:
        """A clean result from the adapter is used unchanged."""
        with mock.patch.object(
            pipeline_mod,
            "has_uncommitted_changes",
            return_value=(False, "could not resolve the decision-log exclusion"),
        ) as dirty_mock:
            dirty, err = pipeline_mod._dirty_with_exclusion(
                REPO, (ExcludedPath(Path("/state")),)
            )

        assert dirty is False, f"expected the adapter result but got {dirty!r}"
        assert err is None, f"expected no error but got {err!r}"
        dirty_mock.assert_called_once_with(
            REPO, exclude=(ExcludedPath(Path("/state")),)
        )
        assert "Could not resolve the decision-log exclusion" in caplog.text

    def test_git_error_is_returned(self) -> None:
        """A missing result is a Git failure, not an exclusion warning."""
        with mock.patch.object(
            pipeline_mod, "changed_files", return_value=(None, "git failed")
        ) as files_mock:
            files, err = pipeline_mod._changed_files_with_exclusion(
                REPO, "abc123", (ExcludedPath(Path("/state")),)
            )

        assert files is None
        assert err == "git failed"
        files_mock.assert_called_once_with(
            REPO, "abc123", exclude=(ExcludedPath(Path("/state")),)
        )
