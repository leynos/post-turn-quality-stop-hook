"""Exercise stop-hook pipeline: evaluate_changes, compush_check, blocking."""

from __future__ import annotations

import json
import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import formatting as formatting_mod
from post_turn_quality_stop_hook import github as github_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.git_facts import GitFacts

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["unit-test"], returncode=returncode, stdout=stdout, stderr=stderr
    )


REPO = Path("/fake/repo")
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


# ---------------------------------------------------------------------------
# TestCompushCheck

# ---------------------------------------------------------------------------


class TestCompushCheck:
    """Tests for compush_check()."""

    def test_dirty_with_upstream(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Dirty tree + upstream -> block with push message."""
        with (
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(True, None)
            ),
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
        ):
            rc = pipeline_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block", (
            f"expected block decision but got {out['decision']!r}"
        )
        assert "Please commit and push to origin/feature" in out["reason"], (
            f"expected commit/push reminder but got {out['reason']!r}"
        )

    def test_dirty_no_upstream(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Dirty tree + no upstream -> block with fallback text."""
        with (
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(True, None)
            ),
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=(None, "no upstream")
            ),
        ):
            rc = pipeline_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block", (
            f"expected block decision but got {out['decision']!r}"
        )
        assert "origin (upstream not configured)" in out["reason"], (
            f"expected upstream fallback in reason but got {out['reason']!r}"
        )

    def test_clean_tree(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Clean tree -> no output, exit 0."""
        with (
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(
                pipeline_mod, "has_unpushed_commits", return_value=(False, None)
            ),
        ):
            rc = pipeline_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        assert capsys.readouterr().out == "", "expected no hook output for clean tree"

    def test_error_checking_changes(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Error from has_uncommitted_changes -> silent exit 0."""
        with (
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=("origin/main", None)
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(None, "oops")
            ),
        ):
            rc = pipeline_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        assert capsys.readouterr().out == "", (
            "expected no hook output when change check errors are suppressed"
        )

    def test_clean_tree_with_unpushed_commits(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Clean tree + ahead of upstream -> block with push-only message."""
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
            rc = pipeline_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block", (
            f"expected block decision but got {out['decision']!r}"
        )
        assert "Please push committed changes to origin/feature" in out["reason"], (
            f"expected push reminder but got {out['reason']!r}"
        )

    def test_clean_tree_no_upstream(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Clean tree + no upstream -> no ahead check and no output."""
        with (
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=(None, "no upstream")
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(pipeline_mod, "has_unpushed_commits") as mock_ahead,
        ):
            rc = pipeline_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        mock_ahead.assert_not_called()
        assert capsys.readouterr().out == "", (
            "expected no hook output when upstream is unavailable"
        )

    def test_error_checking_unpushed_commits(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ahead check errors stay silent to preserve hook contract."""
        with (
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(
                pipeline_mod, "has_unpushed_commits", return_value=(None, "oops")
            ),
        ):
            rc = pipeline_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        assert capsys.readouterr().out == "", (
            "expected no hook output when ahead check errors are suppressed"
        )


# ---------------------------------------------------------------------------
# parse_env - compush flag

# ---------------------------------------------------------------------------


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

    def test_tracked_branch_protection_strips_matching_slash_remote(self) -> None:
        """The actual remote prefix is stripped before branch comparison."""
        branch = pipeline_mod._tracked_branch_name(
            "team/fork/main", "team/fork", ("team/fork",)
        )

        assert branch == "main"

    def test_tracked_branch_protection_strips_non_primary_slash_remote(self) -> None:
        """Configured remotes identify protected upstreams beyond primary remote."""
        branch = pipeline_mod._tracked_branch_name(
            "team/fork/main",
            "origin",
            ("origin", "team/fork"),
        )

        assert branch == "main"

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


class TestPrRebaseCheck:
    """Tests for the pull-request rebase gate."""

    def test_pr_base_ahead_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
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
                return_value=({"check-fmt", "lint", "typecheck"}, None),
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
        assert "`make typecheck`" in payload["reason"]

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
        state = state_mod.HookState()
        rc = formatting_mod.fail_state(state, "something went wrong")
        assert state.ok is False, f"expected ok=False but was {state.ok!r}"
        assert state.error == "something went wrong", (
            f"expected error message but got {state.error!r}"
        )
        assert rc == 0, f"expected return 0 but got {rc!r}"

    def test_none_message(self) -> None:
        state = state_mod.HookState()
        formatting_mod.fail_state(state, None)
        assert state.ok is False
        assert state.error is None, f"expected error=None but got {state.error!r}"


# ---------------------------------------------------------------------------
# format_reason

# ---------------------------------------------------------------------------


class TestFormatReason:
    """Tests for format_reason()."""

    def test_returns_non_empty_string(self) -> None:
        state = state_mod.HookState()
        reason = formatting_mod.format_reason(state)
        assert isinstance(reason, str), f"expected str but got {type(reason)}"
        assert len(reason) > 0, "expected non-empty reason string"

    def test_contains_base_ref(self) -> None:
        state = state_mod.HookState(base_ref="origin/main")
        reason = formatting_mod.format_reason(state)
        assert "origin/main" in reason, (
            f"expected base_ref in reason but got {reason!r}"
        )

    def test_contains_error_message(self) -> None:
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
        state = state_mod.HookState()
        formatting_mod.block_and_print(state)
        out = json.loads(capsys.readouterr().out)
        assert isinstance(out, dict), f"expected JSON object but got {type(out)}"

    def test_json_has_decision_and_reason(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = state_mod.HookState()
        formatting_mod.block_and_print(state)
        out = json.loads(capsys.readouterr().out)
        assert "decision" in out, f"expected 'decision' key in {out}"
        assert "reason" in out, f"expected 'reason' key in {out}"

    def test_decision_is_block(self, capsys: pytest.CaptureFixture[str]) -> None:
        state = state_mod.HookState()
        formatting_mod.block_and_print(state)
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block", (
            f"expected block decision but got {out['decision']!r}"
        )

    def test_returns_zero(self) -> None:
        state = state_mod.HookState()
        rc = formatting_mod.block_and_print(state)
        assert rc == 0, f"expected return 0 but got {rc!r}"


# ---------------------------------------------------------------------------
# targets_for_categories
