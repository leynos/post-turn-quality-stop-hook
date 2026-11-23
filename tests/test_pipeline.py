"""Cover stop-check orchestration, branch-state and PR-rebase gates.

The tests also cover error handling and block-output formatting. The legacy
``compush_check`` helper is tested separately as a downstream-compatibility
API, not as an active gate.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st
from quality_support import command as _command

from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import formatting as formatting_mod
from post_turn_quality_stop_hook import github as github_mod
from post_turn_quality_stop_hook import hook as hook_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import quality as quality_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook import upstream as upstream_mod
from post_turn_quality_stop_hook import upstream_port as upstream_port_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.git_facts import GitFacts

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stable_checkout_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the Git adapter a readable checkout for the fake repository.

    The adapter reads the current branch and HEAD around its fetch so it can
    refuse a comparison whose two sides belong to different branches. These
    tests drive the adapter against a non-existent repository path, so those
    probes are answered here; tests that assert the guard itself override
    them.
    """
    monkeypatch.setattr(upstream_mod, "current_branch", lambda _repo: ("feature", None))
    monkeypatch.setattr(
        upstream_mod, "run", lambda *_args, **_kwargs: _completed(0, stdout="head\n")
    )


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess with the given return code and output."""
    return subprocess.CompletedProcess(
        args=["unit-test"], returncode=returncode, stdout=stdout, stderr=stderr
    )


REPO = Path("/fake/repo")


def _evaluation(*, blocked: bool = False) -> state_mod.QualityEvaluation:
    """Build a passing or blocked quality evaluation for orchestration tests."""
    commands = (_command(exit_code=1),) if blocked else (_command(),)
    return state_mod.QualityEvaluation(
        categories=frozenset({"code"}),
        build_driver="make",
        targets_requested=("check-fmt", "lint", "typecheck"),
        targets_present=("check-fmt", "lint", "typecheck"),
        commands=commands,
    )


REPO = Path("/fake/repo")


URL_UPSTREAM = "https://example.invalid/hub.git"


def _split_upstream_ref(upstream_ref: str) -> tuple[str, str]:
    """Split a rendered upstream ref into its remote and branch names."""
    remote, _separator, branch = upstream_ref.rpartition("/")
    return remote, branch


def _git_source(
    repo: Path, facts: GitFacts | None, config: Config
) -> upstream_port_mod.UpstreamHeadSource:
    """Build the Git adapter with the configuration's upstream settings."""
    return upstream_mod.GitUpstreamHeadSource(
        repo,
        facts,
        refresh_upstream=config.unpushed_commits_refresh_upstream,
        fetch_timeout_seconds=config.unpushed_commits_fetch_timeout_seconds,
    )


def _source_factory(
    repo: Path, facts: GitFacts | None
) -> upstream_port_mod.UpstreamHeadSource:
    """Build the default Git adapter for ``run_stop_checks`` tests."""
    return _git_source(repo, facts, Config())


def _refresh_unpushed(
    state: state_mod.HookState,
    options: state_mod.StopCheckOptions,
    source: upstream_port_mod.UpstreamHeadSource | None = None,
) -> pipeline_mod.BranchStateGateDecision | upstream_port_mod.UpstreamComparison:
    """Collect the refreshed upstream through the given or Git-backed port."""
    origin = source or _git_source(REPO, state.git_facts, options.config)
    return pipeline_mod.refresh_upstream_head(REPO, state.git_facts, options, origin)


def _decide_unpushed(
    state: state_mod.HookState,
    options: state_mod.StopCheckOptions,
    source: upstream_port_mod.UpstreamHeadSource | None = None,
) -> pipeline_mod.BranchStateGateDecision:
    """Refresh the upstream, then decide the unpushed gate."""
    return pipeline_mod.unpushed_commits_gate(_refresh_unpushed(state, options, source))


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
                _source_factory,
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
                _source_factory,
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
                _source_factory,
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
                _source_factory,
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
                _source_factory,
            )
        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        mock_evaluate.assert_not_called()
        mock_uncommitted.assert_called_once()
        mock_unpushed.assert_called_once()
        mock_rebase.assert_called_once()


class TestBranchStateGates:
    """Tests for uncommitted and unpushed gate rendering."""

    def _state_with_upstream(
        self,
        upstream_ref: str | None,
        *,
        upstream: tuple[str | None, str | None] | None = None,
        upstream_error: str | None = None,
    ) -> state_mod.HookState:
        """Return hook state with the requested upstream facts."""
        if upstream is None:
            upstream = (
                _split_upstream_ref(upstream_ref) if upstream_ref else (None, None)
            )
        upstream_remote, upstream_branch = upstream
        return state_mod.HookState(
            git_facts=GitFacts(
                primary_remote="origin",
                upstream_ref=upstream_ref,
                pr_base_local_ref=None,
                local_base_commit=None,
                three_way_merge_is_configured=False,
                upstream_remote=upstream_remote,
                upstream_branch=upstream_branch,
                upstream_merge_ref=(
                    f"refs/heads/{upstream_branch}"
                    if upstream_branch is not None
                    else None
                ),
                upstream_error=upstream_error,
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

    def test_uncommitted_changes_gate_blocks_on_detached_head(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A detached HEAD is unprotected, so dirty work still blocks."""
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=(None, None)
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
                upstream_mod, "remote_names", return_value=(["origin"], None)
            ),
            mock.patch.object(
                upstream_mod,
                "fetch_upstream_head",
                return_value=("upstream-head", None),
            ),
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(True, None)
            ),
        ):
            refresh = _refresh_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )
            decision = pipeline_mod.unpushed_commits_gate(refresh)
            assert isinstance(refresh, upstream_port_mod.UpstreamComparison), (
                "the gate must decide from a refreshed comparison"
            )
            pipeline_mod._emit_unpushed_block(refresh)
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
            mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed,
        ):
            blocked = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
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
            mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed,
        ):
            decision = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
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
        state = self._state_with_upstream("origin/main", upstream=("origin", "main"))
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed,
        ):
            blocked = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )

        assert blocked.should_block is False
        assert blocked.outcome == "skip"
        assert blocked.matched_branch == "main"
        assert blocked.match_kind == "upstream"
        unpushed.assert_not_called()
        assert capsys.readouterr().out == ""

    def test_unpushed_commits_gate_skips_protected_slash_remote_upstream(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A slash-containing remote still resolves a protected upstream."""
        state = self._state_with_upstream(
            "team/fork/main", upstream=("team/fork", "main")
        )
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed,
        ):
            decision = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
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
                upstream_mod, "remote_names", return_value=(["origin"], None)
            ),
            mock.patch.object(
                upstream_mod,
                "fetch_upstream_head",
                return_value=("upstream-head", None),
            ),
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(True, None)
            ),
        ):
            refresh = _refresh_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )
            blocked = pipeline_mod.unpushed_commits_gate(refresh)
            assert isinstance(refresh, upstream_port_mod.UpstreamComparison), (
                "the gate must decide from a refreshed comparison"
            )
            pipeline_mod._emit_unpushed_block(refresh)

        payload = json.loads(capsys.readouterr().out)
        assert blocked.should_block is True
        assert payload["decision"] == "block"
        assert "origin/feature" in payload["reason"]

    def test_unpushed_commits_gate_blocks_with_url_upstream(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A URL-valued upstream is resolved and still blocks when ahead."""
        state = self._state_with_upstream(
            None,
            upstream=(URL_UPSTREAM, "feature"),
        )
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod, "remote_names", return_value=(["origin"], None)
            ),
            mock.patch.object(
                upstream_mod,
                "fetch_upstream_head",
                return_value=("remote-head", None),
            ) as fetch,
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(True, None)
            ),
        ):
            refresh = _refresh_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )
            decision = pipeline_mod.unpushed_commits_gate(refresh)
            assert isinstance(refresh, upstream_port_mod.UpstreamComparison), (
                "the gate must decide from a refreshed comparison"
            )
            pipeline_mod._emit_unpushed_block(refresh)

        fetch.assert_called_once_with(
            REPO,
            URL_UPSTREAM,
            "refs/heads/feature",
            options=upstream_mod.FetchOptions(timeout=10.0),
        )
        payload = json.loads(capsys.readouterr().out)
        assert decision.should_block is True
        assert f"feature on {URL_UPSTREAM}" in payload["reason"]

    def test_unpushed_commits_gate_redacts_url_credentials(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A tokenised upstream URL never reaches the block payload."""
        remote = "https://user:secret@example.invalid/hub.git"
        state = self._state_with_upstream(None, upstream=(remote, "feature"))
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod, "remote_names", return_value=(["origin"], None)
            ),
            mock.patch.object(
                upstream_mod,
                "fetch_upstream_head",
                return_value=("remote-head", None),
            ),
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(True, None)
            ),
        ):
            refresh = _refresh_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )
            decision = pipeline_mod.unpushed_commits_gate(refresh)
            assert isinstance(refresh, upstream_port_mod.UpstreamComparison), (
                "the gate must decide from a refreshed comparison"
            )
            pipeline_mod._emit_unpushed_block(refresh)

        payload = json.loads(capsys.readouterr().out)
        assert decision.should_block is True
        assert "secret" not in payload["reason"]
        assert "feature on https://example.invalid/hub.git" in payload["reason"]

    def test_unpushed_commits_gate_passes_when_remote_matches_head(self) -> None:
        """A refreshed remote head that matches HEAD is not a false block."""
        state = self._state_with_upstream("origin/feature")
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod,
                "fetch_upstream_head",
                return_value=("remote-head", None),
            ),
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(False, None)
            ) as ahead,
        ):
            decision = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )

        ahead.assert_called_once_with(REPO, "remote-head")
        assert decision.should_block is False
        assert decision.outcome == "pass"

    def test_unpushed_commits_gate_fails_open_when_remote_unverifiable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unreachable remote passes with a logged reason, not a block."""
        state = self._state_with_upstream("origin/feature")
        with (
            caplog.at_level("WARNING", logger=pipeline_mod.__name__),
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod,
                "fetch_upstream_head",
                return_value=(None, "git fetch origin feature failed: offline"),
            ),
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            decision = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )

        ahead.assert_not_called()
        assert decision.should_block is False
        assert decision.outcome == "pass"
        assert decision.error is not None
        assert decision.error.message == "git fetch origin feature failed: offline"
        assert any(
            "could not verify the upstream" in record.getMessage()
            for record in caplog.records
        )

    def test_unpushed_commits_gate_uses_local_ref_when_refresh_disabled(
        self,
    ) -> None:
        """The opt-out compares against the local remote-tracking ref."""
        state = self._state_with_upstream("origin/feature")
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            config=Config(unpushed_commits_refresh_upstream=False),
        )
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(True, None)
            ) as ahead,
        ):
            decision = _decide_unpushed(state, options)

        fetch.assert_not_called()
        ahead.assert_called_once_with(REPO, "origin/feature")
        assert decision.should_block is True

    def test_unpushed_commits_gate_reports_upstream_resolution_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unreadable upstream config passes with a logged reason."""
        state = self._state_with_upstream(
            None,
            upstream_error="git config --get branch.feature.remote failed",
        )
        with (
            caplog.at_level("WARNING", logger=pipeline_mod.__name__),
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
        ):
            decision = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )

        fetch.assert_not_called()
        assert decision.should_block is False
        assert decision.outcome == "pass"
        assert decision.error is not None
        assert decision.error.message == "git config --get branch.feature.remote failed"
        assert any(
            "could not resolve the upstream" in record.getMessage()
            for record in caplog.records
        )

    def test_unpushed_commits_gate_passes_without_upstream(self) -> None:
        """A branch with no configured upstream has nothing to push."""
        state = self._state_with_upstream(None)
        with mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch:
            decision = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )

        fetch.assert_not_called()
        assert decision.should_block is False
        assert decision.outcome == "pass"

    def test_unpushed_commits_gate_uses_local_upstream_branch(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ``.`` remote names a local branch that is already authoritative."""
        state = self._state_with_upstream(None, upstream=(".", "integration"))
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(True, None)
            ) as ahead,
        ):
            refresh = _refresh_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )
            decision = pipeline_mod.unpushed_commits_gate(refresh)
            assert isinstance(refresh, upstream_port_mod.UpstreamComparison), (
                "the gate must decide from a refreshed comparison"
            )
            pipeline_mod._emit_unpushed_block(refresh)

        payload = json.loads(capsys.readouterr().out)
        fetch.assert_not_called()
        ahead.assert_called_once_with(REPO, "refs/heads/integration")
        assert decision.should_block is True
        assert "integration" in payload["reason"]

    def test_protected_branch_skips_are_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Protected branch skips record bounded branch-state telemetry."""
        state = self._state_with_upstream("origin/main", upstream=("origin", "main"))
        with (
            caplog.at_level("INFO", logger=pipeline_mod.__name__),
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed,
        ):
            decision = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
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
            mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed,
        ):
            decision = _decide_unpushed(state, options)

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
            f"{remote}/{protected_branch}",
            upstream=(remote, protected_branch),
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
            mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed,
        ):
            decision = _decide_unpushed(state, options)

        assert decision.should_block is False
        assert decision.outcome == "skip"
        assert decision.matched_branch == protected_branch
        assert decision.match_kind == "upstream"
        unpushed.assert_not_called()


class TestUnpushedUpstreamOptOut:
    """Opting out of upstream refresh can only use a local ref."""

    def test_url_upstream_with_refresh_disabled_passes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A URL upstream has no local ref, so the gate fails open."""
        state = state_mod.HookState(
            git_facts=GitFacts(
                primary_remote="origin",
                upstream_ref=None,
                pr_base_local_ref=None,
                local_base_commit=None,
                three_way_merge_is_configured=False,
                upstream_remote=URL_UPSTREAM,
                upstream_branch="feature",
                upstream_merge_ref="refs/heads/feature",
            )
        )
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            config=Config(unpushed_commits_refresh_upstream=False),
        )
        with (
            caplog.at_level("WARNING", logger=pipeline_mod.__name__),
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            decision = _decide_unpushed(state, options)

        fetch.assert_not_called()
        ahead.assert_not_called()
        assert decision.should_block is False
        assert decision.outcome == "pass"
        assert decision.error is not None
        assert decision.error.message == (
            "upstream refresh is disabled and the upstream is not a local ref"
        )
        assert any(
            "could not verify the upstream" in record.getMessage()
            for record in caplog.records
        )


def _comparison(  # noqa: PLR0913 - mirrors the value object it builds
    remote: str,
    branch: str,
    *,
    names: tuple[str, ...] | None = (),
    ahead: bool | None = None,
    error: str | None = None,
) -> upstream_port_mod.UpstreamComparison:
    """Build a prepared comparison for the pure decision tests."""
    return upstream_port_mod.UpstreamComparison(
        remote=remote,
        branch=branch,
        remote_names=names,
        ahead=ahead,
        error=error,
    )


class TestUpstreamLabel:
    """The upstream label is a pure rendering of prepared comparison fields."""

    @pytest.mark.parametrize(
        ("comparison", "expected"),
        [
            (
                _comparison("origin", "feature", names=("origin", "upstream")),
                "origin/feature",
            ),
            # A failed remote-name read degrades to the URL form.
            (_comparison("origin", "feature", names=None), "feature on origin"),
            (
                _comparison(URL_UPSTREAM, "feature", names=("origin",)),
                f"feature on {URL_UPSTREAM}",
            ),
            (
                _comparison(
                    "https://user:secret@example.invalid/hub.git",
                    "feature",
                    names=None,
                ),
                "feature on https://example.invalid/hub.git",
            ),
            (_comparison(".", "integration"), "integration"),
            # A malformed URL cannot be rendered safely, so only the branch is
            # shown; no part of the credential-bearing value may reach a
            # payload or a warning.
            (
                _comparison(
                    "https://user:p/ss@example.invalid/hub.git", "feature", names=()
                ),
                "feature",
            ),
        ],
        ids=[
            "remote-name",
            "names-unread",
            "url",
            "url-userinfo",
            "local",
            "malformed-url",
        ],
    )
    def test_renders_prepared_inputs_without_io(
        self, comparison: upstream_port_mod.UpstreamComparison, expected: str
    ) -> None:
        """The label never runs Git, however the remote is spelled."""
        with (
            mock.patch.object(upstream_mod, "run") as run,
            mock.patch.object(upstream_mod, "remote_names") as read_names,
        ):
            label = pipeline_mod._upstream_label(comparison)

        run.assert_not_called()
        read_names.assert_not_called()
        assert label == expected


class TestUnpushedDecisionSeam:
    """The unpushed decision is a pure mapping from refreshed state."""

    def test_prepared_comparison_blocks_without_collecting_again(self) -> None:
        """A prepared comparison blocks without a fetch or a second comparison."""
        refresh = _comparison("origin", "feature", names=("origin",), ahead=True)
        with (
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            decision = pipeline_mod.unpushed_commits_gate(refresh)

        fetch.assert_not_called()
        ahead.assert_not_called()
        assert decision.should_block is True
        assert decision.payload is None, "the decision must not render a payload"
        assert decision.error is None

    def test_prepared_comparison_passes_without_collecting_again(self) -> None:
        """A prepared comparison with no unpushed commits passes."""
        refresh = _comparison("origin", "feature", names=("origin",), ahead=False)
        with (
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            decision = pipeline_mod.unpushed_commits_gate(refresh)

        fetch.assert_not_called()
        ahead.assert_not_called()
        assert decision.should_block is False
        assert decision.outcome == "pass"

    def test_the_gate_never_calls_the_port(self) -> None:
        """The decision takes a value, so it cannot trigger transport work."""
        source = _FakeUpstreamSource(
            _comparison("origin", "feature", names=("origin",), ahead=True)
        )
        decision = pipeline_mod.unpushed_commits_gate(_comparison("origin", "feature"))

        assert source.calls == 0, "constructing the decision must not read the port"
        assert decision.should_block is False

    def test_emits_the_block_from_the_prepared_comparison(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The emitter renders the same comparison the gate decided from."""
        pipeline_mod._emit_unpushed_block(
            _comparison("origin", "feature", names=("origin",), ahead=True)
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload["decision"] == "block"
        assert "origin/feature" in payload["reason"]

    def test_a_malformed_credential_url_reaches_no_payload(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unrenderable URL is replaced by the branch name alone."""
        pipeline_mod._emit_unpushed_block(
            _comparison(
                "https://user:p/ss@example.invalid/hub.git", "feature", names=()
            )
        )

        payload = json.loads(capsys.readouterr().out)
        assert "ss@" not in payload["reason"], (
            "no part of the configured URL may survive an unsafe render"
        )
        assert "`feature`" in payload["reason"]


class _FakeUpstreamSource:
    """Stand-in upstream port that records whether it was consulted."""

    def __init__(self, comparison: upstream_port_mod.UpstreamComparison | None) -> None:
        """Serve ``comparison`` and start the consultation counter at zero."""
        self._comparison = comparison
        self.calls = 0

    def refresh_comparison(self) -> upstream_port_mod.UpstreamComparison | None:
        """Return the prepared comparison and count the read."""
        self.calls += 1
        return self._comparison


class TestUnpushedUpstreamPort:
    """The gate decides from an injected port without Git or the network."""

    @staticmethod
    def _state(
        remote: str | None, branch: str | None, upstream_ref: str | None = None
    ) -> state_mod.HookState:
        """Return hook state whose upstream facts describe the given target."""
        return state_mod.HookState(
            git_facts=GitFacts(
                primary_remote="origin",
                upstream_ref=upstream_ref,
                pr_base_local_ref=None,
                local_base_commit=None,
                three_way_merge_is_configured=False,
                upstream_remote=remote,
                upstream_branch=branch,
                upstream_merge_ref=(
                    f"refs/heads/{branch}" if branch is not None else None
                ),
            )
        )

    def test_prepared_block_is_decided_from_the_port(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An ahead comparison blocks without any Git call."""
        source = _FakeUpstreamSource(
            _comparison("origin", "feature", names=("origin",), ahead=True)
        )
        with (
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            refresh = _refresh_unpushed(
                self._state("origin", "feature", "origin/feature"),
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                source,
            )
            decision = pipeline_mod.unpushed_commits_gate(refresh)
            assert isinstance(refresh, upstream_port_mod.UpstreamComparison), (
                "the port must supply the comparison the gate decides from"
            )
            pipeline_mod._emit_unpushed_block(refresh)

        fetch.assert_not_called()
        ahead.assert_not_called()
        assert source.calls == 1
        assert decision.should_block is True
        payload = json.loads(capsys.readouterr().out)
        assert "origin/feature" in payload["reason"]

    def test_unavailable_comparison_passes_and_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unverifiable upstream passes with the port's error."""
        source = _FakeUpstreamSource(
            _comparison(
                "origin",
                "feature",
                names=("origin",),
                ahead=None,
                error="git fetch origin feature failed: offline",
            )
        )
        with (
            caplog.at_level("WARNING", logger=pipeline_mod.__name__),
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
        ):
            decision = _decide_unpushed(
                self._state("origin", "feature", "origin/feature"),
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                source,
            )

        assert decision.should_block is False
        assert decision.outcome == "pass"
        assert decision.error is not None
        assert decision.error.message == "git fetch origin feature failed: offline"
        assert any(
            "could not verify the upstream origin/feature" in record.getMessage()
            for record in caplog.records
        )

    def test_local_upstream_comparison_passes(self) -> None:
        """A local upstream comparison passes when nothing is ahead."""
        source = _FakeUpstreamSource(_comparison(".", "integration", ahead=False))
        with mock.patch.object(
            pipeline_mod, "current_branch", return_value=("feature", None)
        ):
            decision = _decide_unpushed(
                self._state(".", "integration"),
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                source,
            )

        assert decision.should_block is False
        assert decision.outcome == "pass"

    def test_disabled_gate_skips_without_consulting_the_port(self) -> None:
        """The gate opt-out returns a skip and never reads the port."""
        source = _FakeUpstreamSource(
            _comparison("origin", "feature", names=("origin",), ahead=True)
        )
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            config=Config(gate_unpushed_commits=False),
        )
        decision = _decide_unpushed(
            self._state("origin", "feature", "origin/feature"), options, source
        )

        assert source.calls == 0
        assert decision.outcome == "skip"


class _ModuleImportCollector(ast.NodeVisitor):
    """Collect imports made outside function and class bodies.

    Imports inside an ``if TYPE_CHECKING:`` block or another module-level
    conditional still describe the module's import graph, while a deliberately
    deferred import inside a function does not. A ``from package import
    member`` statement imports a module as well as a name, and a relative
    import names the same module as its absolute form, so both are recorded
    under the qualified name a boundary assertion can recognise.
    """

    def __init__(self, package: str) -> None:
        """Record imports relative to ``package``."""
        self.package = package
        self.imported: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Skip a function body's deferred imports."""

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Skip a coroutine body's deferred imports."""

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Skip a class body, which holds no module-level imports."""

    def visit_Import(self, node: ast.Import) -> None:
        """Record a plain ``import`` statement."""
        self.imported.update(alias.name for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Record the module and members named by a ``from`` import."""
        module = self._resolve(node.module, node.level)
        if module is None:
            return
        self.imported.add(module)
        self.imported.update(f"{module}.{alias.name}" for alias in node.names)

    def _resolve(self, module: str | None, level: int) -> str | None:
        """Resolve a possibly relative module name against the package."""
        if level == 0:
            return module
        parts = self.package.split(".") if self.package else []
        parents = level - 1
        if parents > len(parts):
            return None
        base = parts[: len(parts) - parents]
        if module:
            base = [*base, module]
        return ".".join(base) or None


class TestUpstreamPortBoundary:
    """The domain-facing port stays independent of the Git adapter."""

    @staticmethod
    def _module_imports(module: object) -> set[str]:
        """Return the modules imported outside function bodies by ``module``."""
        module_file = getattr(module, "__file__", None)
        package = getattr(module, "__package__", "")
        assert isinstance(module_file, str), "the module must be a source module"
        assert isinstance(package, str), "the module must declare a package"
        tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
        collector = _ModuleImportCollector(package)
        collector.visit(tree)
        return collector.imported

    @staticmethod
    def _functions_calling(module: object, attribute: str) -> set[str]:
        """Return the top-level functions that reference ``attribute``."""
        module_file = getattr(module, "__file__", None)
        assert isinstance(module_file, str), "the module must be a source module"
        tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
        return {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(sub, ast.Attribute) and sub.attr == attribute
                for sub in ast.walk(node)
            )
        }

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (
                "import post_turn_quality_stop_hook.upstream",
                {"post_turn_quality_stop_hook.upstream"},
            ),
            (
                "from post_turn_quality_stop_hook import upstream",
                {
                    "post_turn_quality_stop_hook",
                    "post_turn_quality_stop_hook.upstream",
                },
            ),
            (
                "from . import upstream",
                {
                    "post_turn_quality_stop_hook",
                    "post_turn_quality_stop_hook.upstream",
                },
            ),
            (
                "from .upstream import GitUpstreamHeadSource",
                {
                    "post_turn_quality_stop_hook.upstream",
                    "post_turn_quality_stop_hook.upstream.GitUpstreamHeadSource",
                },
            ),
        ],
        ids=["module", "package-member", "relative-member", "relative-module"],
    )
    def test_records_every_import_form_under_its_qualified_name(
        self, source: str, expected: set[str]
    ) -> None:
        """Each spelling of the same dependency is visible to the boundary."""
        collector = _ModuleImportCollector("post_turn_quality_stop_hook")

        collector.visit(ast.parse(source))

        assert collector.imported == expected

    def test_function_body_imports_are_ignored(self) -> None:
        """A deferred import inside a function is not a module-scope import."""
        collector = _ModuleImportCollector("post_turn_quality_stop_hook")

        collector.visit(
            ast.parse(
                "def load():\n    from post_turn_quality_stop_hook import upstream\n"
            )
        )

        assert collector.imported == set()

    def test_port_module_imports_no_adapter_or_transport_module(self) -> None:
        """The port depends on no Git, process, or redaction module."""
        banned = {
            "post_turn_quality_stop_hook.git",
            "post_turn_quality_stop_hook.process",
            "post_turn_quality_stop_hook.redaction",
            "post_turn_quality_stop_hook.upstream",
            "subprocess",
        }

        assert not self._module_imports(upstream_port_mod) & banned

    def test_pipeline_imports_the_port_but_not_the_adapter(self) -> None:
        """The decision path imports only the port at module scope."""
        imported = self._module_imports(pipeline_mod)

        assert "post_turn_quality_stop_hook.upstream_port" in imported
        assert "post_turn_quality_stop_hook.upstream" not in imported
        assert "post_turn_quality_stop_hook.upstream.GitUpstreamHeadSource" not in (
            imported
        )

    def test_the_composition_root_is_where_the_adapter_is_imported(self) -> None:
        """The entry point, not the pipeline, builds the Git adapter."""
        imported = self._module_imports(hook_mod)

        assert "post_turn_quality_stop_hook.upstream" in imported

    def test_only_the_refresh_command_reads_the_port(self) -> None:
        """The decision never calls the port; only the refresh step does."""
        assert self._functions_calling(pipeline_mod, "refresh_comparison") == {
            "refresh_upstream_head"
        }


class TestTargetEnumerationLogging:
    """Target read failures remain visible at the quality execution boundary."""

    def test_enumeration_failure_logs_once(
        self, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A read error is returned once without printing from the unit."""
        error = "could not read /fake/repo/Makefile: permission denied"
        state = state_mod.HookState(changed_files=("app.py",))
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        with mock.patch.object(
            quality_mod,
            "get_build_targets",
            return_value=HookError("get_make_targets", error),
        ):
            result = quality_mod.evaluate_changes(state, REPO, 12000, driver)

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
        records = [r for r in caplog.records if r.name == quality_mod.__name__]
        assert [(r.levelname, r.getMessage()) for r in records] == [
            ("WARNING", f"Could not enumerate build targets: {error}")
        ], "expected exactly one enumeration warning"
        assert capsys.readouterr().out == "", "expected the unit to stay silent"

    def test_disabled_quality_gate_skips_driver_selection(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A disabled quality gate returns quietly without selecting a driver."""
        state = state_mod.HookState(changed_files=("app.py",))
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

    def test_returns_enumeration_failure_report(
        self, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The quality stage returns the blocking report without printing it."""
        error = "could not read /fake/repo/Makefile: permission denied"
        state = state_mod.HookState(changed_files=("app.py",))
        options = state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        with (
            mock.patch.object(pipeline_mod, "select_build_driver", return_value=driver),
            mock.patch.object(
                quality_mod,
                "get_build_targets",
                return_value=HookError("get_make_targets", error),
            ),
        ):
            result = pipeline_mod.run_quality_checks_if_needed(state, REPO, options)

        match result:
            case state_mod.HookState():
                pass
            case _:
                pytest.fail(f"expected a blocking report but got {result!r}")
        assert result.ok is False, f"expected a failed report but got {result.ok!r}"
        assert result.error == error, (
            f"expected {error!r} in the report but got {result.error!r}"
        )
        assert result.targets_requested == ("check-fmt", "lint", "typecheck"), (
            f"expected requested targets in the report but got "
            f"{result.targets_requested!r}"
        )
        records = [r for r in caplog.records if r.name == quality_mod.__name__]
        assert [(r.levelname, r.getMessage()) for r in records] == [
            ("WARNING", f"Could not enumerate build targets: {error}")
        ], "expected the quality unit to log the warning once"
        assert capsys.readouterr().out == "", "expected the stage to stay silent"


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

    def test_collection_fields_reject_mutation(self) -> None:
        """Snapshots expose only immutable collection types."""
        state = state_mod.HookState(
            changed_files=("src/a.py",),
            categories=frozenset({"code"}),
            targets_requested=("check-fmt",),
            commands=(_command(),),
        )

        with pytest.raises(AttributeError):
            state.categories.add("markdown")  # ty: ignore[unresolved-attribute]
        with pytest.raises(AttributeError):
            state.changed_files.append("src/b.py")  # ty: ignore[unresolved-attribute]
        with pytest.raises(AttributeError):
            state.commands.append(_command())  # ty: ignore[unresolved-attribute]

    def test_recorded_commands_remain_frozen(self) -> None:
        """A recorded command cannot change the snapshot's blocked result."""
        evaluation = state_mod.QualityEvaluation(
            categories=frozenset({"code"}),
            commands=(_command(exit_code=1),),
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            evaluation.commands[0].exit_code = 0  # ty: ignore[invalid-assignment]
        assert evaluation.blocked is True, (
            "a failed recorded command must keep the evaluation blocking"
        )

    def test_source_mutation_does_not_change_snapshot(self) -> None:
        """Mutating the input list after construction leaves the snapshot intact."""
        files = ["src/a.py"]
        state = state_mod.HookState(changed_files=tuple(files))

        files.append("src/b.py")

        assert state.changed_files == ("src/a.py",), (
            f"expected the snapshot to keep its own files but got "
            f"{state.changed_files!r}"
        )


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
