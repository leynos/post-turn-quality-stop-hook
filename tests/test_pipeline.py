"""Cover stop-check orchestration, branch-state and PR-rebase gates.

The tests also cover error handling and block-output formatting. The legacy
``compush_check`` helper is tested separately as a downstream-compatibility
API, not as an active gate.
"""

from __future__ import annotations

import ast
# refused_by: merge_ladder_exhausted · collision: none (both sides edited, nowhere the same lines)
import dataclasses
import json
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import typing as typ
from pathlib import Path
from unittest import mock

import pytest
from branch_support import (
    FakeBranchState,
    FakePullRequestLookup,
    FakeTargetEnumerator,
    gate_ports,
    pull_request,
)
from decision_log_support import _make_log, _read_records, _settings
from hypothesis import given
from hypothesis import strategies as st
from observer_support import RecordingObserver as _RecordingObserver
from observer_support import no_warnings as _no_warnings
from quality_support import (
    FakeRepositoryOperations,
    ScriptedRepositoryOperationsFactory,
    UnusedRepositoryFacts,
    enumerated,
    repository_factory,
)
from quality_support import command as _command

from post_turn_quality_stop_hook import branch_gates as branch_gates_mod
from post_turn_quality_stop_hook import branch_state_port as branch_state_mod
from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import formatting as formatting_mod
from post_turn_quality_stop_hook import hook as hook_mod

# Reporting telemetry moved out of `pipeline` into `observation`; its log
# records carry that module's name.
from post_turn_quality_stop_hook import observation as observation_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import quality as quality_mod
from post_turn_quality_stop_hook import repository_port as repository_port_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook import upstream as upstream_mod
from post_turn_quality_stop_hook import upstream_port as upstream_port_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.git_facts import GitFacts
from post_turn_quality_stop_hook.observer import NullObserver
from post_turn_quality_stop_hook.recording import InvocationLog

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook.repository_facts import RepositoryFactsSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FixedRepositoryFacts:
    """Repository-fact source for a run whose observer keeps the facts."""

    def current_branch(self, repo: Path) -> tuple[str | None, HookError | None]:
        """Return a prepared branch name."""
        del repo
        return "feature", None

    def head_commit(self, repo: Path) -> tuple[str | None, HookError | None]:
        """Return a prepared commit."""
        del repo
        return "abc123", None

    def has_uncommitted_changes(
        self, repo: Path
    ) -> tuple[bool | None, HookError | None]:
        """Return a prepared dirtiness answer."""
        del repo
        return False, None

    def is_ahead_of_upstream(
        self, repo: Path, upstream_ref: str
    ) -> tuple[bool | None, HookError | None]:
        """Return a prepared unpushed answer."""
        del repo, upstream_ref
        return False, None


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
        on_warning=_no_warnings,
    )


def _source_factory(
    repo: Path, facts: GitFacts | None, _observer: object
) -> upstream_port_mod.UpstreamHeadSource:
    """Build the default Git adapter for ``run_stop_checks`` tests."""
    return _git_source(repo, facts, Config())


def _refresh_unpushed(
    state: state_mod.HookState,
    options: state_mod.StopCheckOptions,
    source: upstream_port_mod.UpstreamHeadSource | None = None,
    branch_state: FakeBranchState | None = None,
) -> pipeline_mod.BranchStateGateDecision | upstream_port_mod.UpstreamComparison:
    """Collect the refreshed upstream through the given or Git-backed port."""
    origin = source or _git_source(REPO, state.git_facts, options.config)
    return pipeline_mod.refresh_upstream_head(
        REPO,
        state.git_facts,
        options,
        origin,
        branch_state=branch_state or FakeBranchState(),
    )


def _decide_unpushed(
    state: state_mod.HookState,
    options: state_mod.StopCheckOptions,
    source: upstream_port_mod.UpstreamHeadSource | None = None,
    branch_state: FakeBranchState | None = None,
) -> pipeline_mod.BranchStateGateDecision:
    """Refresh the upstream, then decide the unpushed gate."""
    refresh = _refresh_unpushed(state, options, source, branch_state)
    return pipeline_mod.unpushed_commits_gate(refresh)


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
    _pass = pipeline_mod.BranchStateGateDecision(gate="test", outcome="pass")

    def _run(  # ruff:ignore[too-many-arguments]  # each prepared boundary is an independent input.
        self,
        operations: FakeRepositoryOperations,
        *,
        driver: driver_mod.BuildDriver | None = None,
        no_driver: bool = False,
        evaluation: state_mod.QualityEvaluation | None = None,
        facts_source: RepositoryFactsSource | None = None,
        invocation: InvocationLog | None = None,
    ) -> tuple[int, ScriptedRepositoryOperationsFactory, dict[str, mock.Mock]]:
        """Run the pipeline with the repository port injected.

        The repository questions arrive through the port, so no Git import is
        patched. The remaining patches are the other boundaries the pipeline
        composes: repository discovery, the base ref, the build driver, and the
        three gates whose own decisions are covered elsewhere.
        """
        factory = repository_factory(operations)
        mocks: dict[str, mock.Mock] = {}
        with (
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(
                pipeline_mod,
                "select_build_driver",
                return_value=None
                if no_driver
                else (self.driver if driver is None else driver),
            ),
            mock.patch.object(
                pipeline_mod,
                "evaluate_changes",
                return_value=_evaluation() if evaluation is None else evaluation,
            ) as evaluate,
            mock.patch.object(
                pipeline_mod, "uncommitted_changes_gate", return_value=self._pass
            ) as uncommitted,
            mock.patch.object(
                pipeline_mod, "unpushed_commits_gate", return_value=self._pass
            ) as unpushed,
            mock.patch.object(
                pipeline_mod, "pr_rebase_check", return_value=0
            ) as rebase,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            mocks.update(
                evaluate=evaluate,
                uncommitted=uncommitted,
                unpushed=unpushed,
                rebase=rebase,
            )
            rc = pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                _source_factory,
                branch_gate_ports=gate_ports(),
                repository_facts_source=(
                    UnusedRepositoryFacts() if facts_source is None else facts_source
                ),
                repository_operations_for=factory,
                warn=_no_warnings,
                invocation=invocation,
            )
        return rc, factory, mocks

    def test_preparation_asks_the_port_for_changed_paths(self) -> None:
        """Preparation asks which paths changed since the merge base."""
        operations = FakeRepositoryOperations(changed=("src/foo.py",))

        rc, factory, _mocks = self._run(operations)

        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        assert operations.bases == ["abc123"], (
            "expected the merge base to be the base asked about, got "
            f"{operations.bases!r}"
        )
        assert factory.roots == [REPO, REPO], (
            "expected preparation and the gate phase to bind the resolved root,"
            f" got {factory.roots!r}"
        )

    def test_a_changed_path_read_failure_blocks_preparation(self) -> None:
        """An unreadable path list blocks before any gate runs."""
        error = HookError("changed_paths_since", "git diff failed")
        operations = FakeRepositoryOperations(changed=None, changed_error=error)
        with (
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(pipeline_mod, "run_branch_state_gates") as gates,
        ):
            rc = pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                _source_factory,
                branch_gate_ports=gate_ports(),
                repository_facts_source=UnusedRepositoryFacts(),
                repository_operations_for=repository_factory(operations),
                warn=_no_warnings,
            )

        assert rc == 0, f"expected a blocking run to exit 0 but got {rc!r}"
        gates.assert_not_called()
        assert operations.calls == ["changed_paths_since"], (
            f"expected only the failing read, got {operations.calls!r}"
        )

    def test_the_uncommitted_gate_receives_the_injected_port(self) -> None:
        """The gate is handed the port, not a repository path or exclusion list."""
        operations = FakeRepositoryOperations(branch="feature", dirty=True)

        rc, _factory, mocks = self._run(operations)

        assert rc == 0
        gate_args, gate_kwargs = mocks["uncommitted"].call_args
        assert gate_args[0] is operations, (
            f"the gate must receive the injected port, got {gate_args!r}"
        )
        assert gate_kwargs == {}, (
            f"the gate takes no branch-state keyword any more, got {gate_kwargs!r}"
        )

    def test_unpushed_gate_runs_after_clean_worktree(self) -> None:
        """Clean working tree -> unpushed gate is evaluated."""
        operations = FakeRepositoryOperations(changed=("src/foo.py",), dirty=False)

        rc, _factory, mocks = self._run(operations)

        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        mocks["uncommitted"].assert_called_once()
        mocks["unpushed"].assert_called_once()
        mocks["rebase"].assert_called_once()

    def test_branch_state_gates_skipped_on_quality_failure(self) -> None:
        """Quality check failure -> branch-state gates are not evaluated."""
        operations = FakeRepositoryOperations(changed=("src/foo.py",))

        rc, _factory, mocks = self._run(
            operations, evaluation=_evaluation(blocked=True)
        )

        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        mocks["uncommitted"].assert_not_called()
        mocks["unpushed"].assert_not_called()
        mocks["rebase"].assert_not_called()
        assert operations.calls == ["changed_paths_since"], (
            f"only preparation should ask the port, got {operations.calls!r}"
        )

    def test_branch_state_gates_run_without_build_driver(self, tmp_path: Path) -> None:
        """No Makefile or Netsukefile skips quality gates in auto mode."""
        # The skip is asserted on the durable record rather than on a logger:
        # the observer is the pipeline's only telemetry channel.
        log = _make_log(_settings(tmp_path))
        operations = FakeRepositoryOperations(changed=("src/foo.py",))

        rc, _factory, mocks = self._run(
            operations,
            no_driver=True,
            facts_source=_FixedRepositoryFacts(),
            invocation=InvocationLog(recorder=log),
        )
        log.finish()

        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        mocks["evaluate"].assert_not_called()
        mocks["uncommitted"].assert_called_once()
        mocks["unpushed"].assert_called_once()
        mocks["rebase"].assert_called_once()
        skips = [
            record
            for record in _read_records(tmp_path / "state")
            if record.get("event") == "quality_gate_skip"
        ]
        assert len(skips) == 1, f"expected one quality skip record, got {skips!r}"
        assert skips[0]["gate"] == "quality_checks"
        assert skips[0]["outcome"] == "skip"

    def test_branch_state_gates_run_when_no_files_changed(self) -> None:
        """Branch-state gates still run when there are no changed files to lint."""
        operations = FakeRepositoryOperations(changed=())

        rc, _factory, mocks = self._run(operations)

        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        mocks["evaluate"].assert_not_called()
        mocks["uncommitted"].assert_called_once()
        mocks["unpushed"].assert_called_once()
        mocks["rebase"].assert_called_once()


class TestBranchStateGates:  # ruff:ignore[too-many-public-methods]  # one test per gate path is the point of the class.
    """Tests for uncommitted and unpushed gate decisions.

    The gates decide from the injected branch-state port, so these tests never
    patch a gate-module attribute and never reach Git. The unpushed cases drive
    the real upstream adapter with its transport functions patched, which is
    the one command step the gate deliberately delegates.
    """

    def _state_with_upstream(
        self,
        upstream_ref: str | None,
        *,
        upstream: tuple[str | None, str | None] | None = None,
        upstream_error: HookError | None = None,
        branch: str | None = "feature",
    ) -> state_mod.HookState:
        """Return hook state with the requested upstream facts.

        ``branch`` is the branch the facts were collected on. It matches the
        branch-state port's answer unless a test says otherwise, so the
        snapshot describes the checked-out branch by default.
        """
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
                branch=branch,
            )
        )

    def test_uncommitted_changes_gate_blocks(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Dirty working tree emits the uncommitted template."""
        repository = FakeRepositoryOperations(dirty=True)

        decision = branch_gates_mod.uncommitted_changes_gate(
            repository,
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
        )
        formatting_mod.emit_decision_payload(decision)

        payload = json.loads(capsys.readouterr().out)
        assert decision.should_block is True
        assert payload["decision"] == "block"
        assert "Please commit outstanding changes" in payload["reason"]
        assert repository.calls == ["current_branch", "has_uncommitted_work"], (
            "the gate must ask the port for the branch, then for dirtiness,"
            f" but asked {repository.calls!r}"
        )

    def test_uncommitted_changes_gate_passes_a_clean_tree(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A clean tree passes and says so without emitting a payload."""
        repository = FakeRepositoryOperations(dirty=False)

        decision = branch_gates_mod.uncommitted_changes_gate(
            repository,
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
        )

        assert decision.outcome == "pass"
        assert decision.reason == "working tree clean"
        assert decision.error is None
        assert capsys.readouterr().out == ""

    def test_uncommitted_changes_gate_blocks_on_detached_head(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A detached HEAD is unprotected, so dirty work still blocks."""
        decision = branch_gates_mod.uncommitted_changes_gate(
            FakeRepositoryOperations(branch=None, dirty=True),
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
        )
        formatting_mod.emit_decision_payload(decision)

        payload = json.loads(capsys.readouterr().out)
        assert decision.should_block is True
        assert payload["decision"] == "block"

    def test_uncommitted_changes_gate_skips_protected_branch(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Protected local branches are not prompted for direct commits."""
        repository = FakeRepositoryOperations(branch="main")

        blocked = branch_gates_mod.uncommitted_changes_gate(
            repository,
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
        )

        assert blocked.should_block is False
        assert blocked.outcome == "skip"
        assert blocked.matched_branch == "main"
        assert "has_uncommitted_work" not in repository.calls, (
            "a protected branch must not be asked about dirtiness"
        )
        assert capsys.readouterr().out == ""

    def test_uncommitted_changes_gate_asks_nothing_when_disabled(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A disabled gate returns before it asks the repository anything."""
        repository = FakeRepositoryOperations(dirty=True)
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            config=Config(gate_uncommitted_changes=False),
        )

        decision = branch_gates_mod.uncommitted_changes_gate(repository, options)

        assert decision.outcome == "skip"
        assert decision.reason == "gate disabled by configuration"
        assert repository.calls == [], (
            f"a disabled gate must ask no question, but asked {repository.calls!r}"
        )
        assert capsys.readouterr().out == ""

    def test_uncommitted_changes_gate_reports_a_branch_read_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unreadable branch passes with the error rather than blocking."""
        error = HookError("current_branch", "git branch failed")
        repository = FakeRepositoryOperations(branch_error=error)

        decision = branch_gates_mod.uncommitted_changes_gate(
            repository,
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
        )

        assert decision.outcome == "pass"
        assert decision.error is error
        assert decision.reason == "git error"
        assert "has_uncommitted_work" not in repository.calls, (
            "an unreadable branch must settle the gate before the dirty query"
        )
        assert capsys.readouterr().out == ""

    def test_uncommitted_changes_gate_reports_a_dirty_read_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unreadable tree passes with the error and never guesses clean."""
        error = HookError("has_uncommitted_work", "git status failed")
        repository = FakeRepositoryOperations(dirty=None, dirty_error=error)

        decision = branch_gates_mod.uncommitted_changes_gate(
            repository,
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
        )

        assert decision.outcome == "pass"
        assert decision.error is error
        assert decision.reason == "git error"
        assert capsys.readouterr().out == ""

    def test_unpushed_commits_gate_blocks(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ahead branch emits the unpushed template."""
        state = self._state_with_upstream("origin/feature")
        with (
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
            decision = branch_gates_mod.unpushed_commits_gate(refresh)
            assert isinstance(refresh, upstream_port_mod.UpstreamComparison), (
                "the gate must decide from a refreshed comparison"
            )
            formatting_mod.emit_unpushed_block(refresh)
        payload = json.loads(capsys.readouterr().out)
        assert decision.should_block is True
        assert payload["decision"] == "block"
        assert "origin/feature" in payload["reason"]

    def test_unpushed_commits_gate_skips_protected_branch(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Protected local branches are not prompted for direct pushes."""
        state = self._state_with_upstream("origin/main")
        with mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed:
            blocked = _decide_unpushed(
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                branch_state=FakeBranchState(branch="main"),
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
        with mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed:
            decision = _decide_unpushed(
                state,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                branch_state=FakeBranchState(branch="main"),
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
        with mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed:
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
        with mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed:
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
            blocked = branch_gates_mod.unpushed_commits_gate(refresh)
            assert isinstance(refresh, upstream_port_mod.UpstreamComparison), (
                "the gate must decide from a refreshed comparison"
            )
            formatting_mod.emit_unpushed_block(refresh)

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
            decision = branch_gates_mod.unpushed_commits_gate(refresh)
            assert isinstance(refresh, upstream_port_mod.UpstreamComparison), (
                "the gate must decide from a refreshed comparison"
            )
            formatting_mod.emit_unpushed_block(refresh)

        fetch.assert_called_once_with(
            REPO,
            URL_UPSTREAM,
            "refs/heads/feature",
            options=upstream_mod.FetchOptions(on_warning=_no_warnings, timeout=10.0),
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
            decision = branch_gates_mod.unpushed_commits_gate(refresh)
            assert isinstance(refresh, upstream_port_mod.UpstreamComparison), (
                "the gate must decide from a refreshed comparison"
            )
            formatting_mod.emit_unpushed_block(refresh)

        payload = json.loads(capsys.readouterr().out)
        assert decision.should_block is True
        assert "secret" not in payload["reason"]
        assert "feature on https://example.invalid/hub.git" in payload["reason"]

    def test_unpushed_commits_gate_passes_when_remote_matches_head(self) -> None:
        """A refreshed remote head that matches HEAD is not a false block."""
        state = self._state_with_upstream("origin/feature")
        with (
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
        self,
    ) -> None:
        """An unreachable remote passes with a warning, not a block."""
        observer = _RecordingObserver()
        state = self._state_with_upstream("origin/feature")
        with (
            mock.patch.object(
                upstream_mod,
                "fetch_upstream_head",
                return_value=(
                    None,
                    HookError(
                        "fetch_upstream_head",
                        "git fetch origin feature failed: offline",
                    ),
                ),
            ),
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            decision = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )
        observation_mod.record_gate_decision(observer, decision)

        ahead.assert_not_called()
        assert decision.should_block is False
        assert decision.outcome == "pass"
        assert decision.error is not None
        assert decision.error.message == "git fetch origin feature failed: offline"
        assert any(
            "could not verify the upstream" in warning for warning in observer.warnings
        ), f"expected the gate warning, got {observer.warnings!r}"
        assert observer.decisions == [
            ("unpushed_commits", "pass", "git error", str(decision.error))
        ]

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
        self,
    ) -> None:
        """An unreadable upstream config passes with a warning."""
        observer = _RecordingObserver()
        state = self._state_with_upstream(
            None,
            upstream_error=HookError(
                "resolve_upstream",
                "git config --get branch.feature.remote failed",
            ),
        )
        with mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch:
            decision = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )
        observation_mod.record_gate_decision(observer, decision)

        fetch.assert_not_called()
        assert decision.should_block is False
        assert decision.outcome == "pass"
        assert decision.error is not None
        assert decision.error.message == "git config --get branch.feature.remote failed"
        assert any(
            "could not resolve the upstream" in warning for warning in observer.warnings
        ), f"expected the gate warning, got {observer.warnings!r}"

    def test_unpushed_commits_gate_skips_without_upstream(self) -> None:
        """A branch with no configured upstream skips the check.

        The users' guide documents an unconfigured upstream as a skipped
        check, so the durable record must not claim a verified pass.
        """
        state = self._state_with_upstream(None)
        with mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch:
            decision = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )

        fetch.assert_not_called()
        assert decision.should_block is False
        assert decision.outcome == "skip", (
            f"an unconfigured upstream must be a skipped check, got {decision!r}"
        )
        assert decision.reason == "no upstream configured"

    @pytest.mark.parametrize(
        "upstream",
        [("origin", None), (None, "feature")],
        ids=["no-branch", "no-remote"],
    )
    def test_unpushed_commits_gate_skips_a_partial_upstream(
        self, upstream: tuple[str | None, str | None]
    ) -> None:
        """A remote or branch missing from the facts skips without a block."""
        state = self._state_with_upstream("origin/feature", upstream=upstream)
        with mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch:
            decision = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )

        fetch.assert_not_called()
        assert decision.should_block is False
        assert decision.outcome == "skip"
        assert decision.reason == "no upstream configured"

    def test_unpushed_commits_gate_skips_when_facts_are_missing(self) -> None:
        """A failed fact collection leaves the gate skipped, never blocked."""
        state = state_mod.HookState(git_facts=None)
        with mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch:
            decision = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )

        fetch.assert_not_called()
        assert decision.should_block is False
        assert decision.outcome == "skip"
        assert decision.reason == "no upstream configured"

    def test_unpushed_commits_gate_skips_when_the_refresh_has_no_comparison(
        self,
    ) -> None:
        """A port that reports no upstream skips rather than claiming a pass."""
        state = self._state_with_upstream("origin/feature")

        class _NoUpstream:
            """Port reporting that the branch has no configured upstream."""

            def refresh_comparison(self) -> None:
                """Return no comparison."""

        decision = _decide_unpushed(
            state,
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            source=typ.cast("upstream_port_mod.UpstreamHeadSource", _NoUpstream()),
        )

        assert decision.should_block is False
        assert decision.outcome == "skip"
        assert decision.reason == "no upstream configured"

    def test_unpushed_commits_gate_uses_local_upstream_branch(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ``.`` remote names a local branch that is already authoritative."""
        state = self._state_with_upstream(None, upstream=(".", "integration"))
        with (
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(True, None)
            ) as ahead,
        ):
            refresh = _refresh_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )
            decision = branch_gates_mod.unpushed_commits_gate(refresh)
            assert isinstance(refresh, upstream_port_mod.UpstreamComparison), (
                "the gate must decide from a refreshed comparison"
            )
            formatting_mod.emit_unpushed_block(refresh)

        payload = json.loads(capsys.readouterr().out)
        fetch.assert_not_called()
        ahead.assert_called_once_with(REPO, "refs/heads/integration")
        assert decision.should_block is True
        assert "integration" in payload["reason"]

    def test_protected_branch_skips_are_recorded(self) -> None:
        """Protected branch skips record bounded branch-state telemetry."""
        observer = _RecordingObserver()
        state = self._state_with_upstream("origin/main", upstream=("origin", "main"))
        with mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed:
            decision = _decide_unpushed(
                state, state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
            )
        observation_mod.record_gate_decision(observer, decision)

        assert decision.outcome == "skip"
        unpushed.assert_not_called()
        telemetry = dict(observer.telemetry)
        assert telemetry["branch_state_gate_skip"] == {
            "gate": "unpushed_commits",
            "outcome": "skip",
            "matched_branch": "main",
            "match_kind": "upstream",
        }

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
        with mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed:
            decision = _decide_unpushed(
                state, options, branch_state=FakeBranchState(branch=protected_branch)
            )

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
            branch="topic",
        )
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            config=Config(protected_branches=protected_branches),
        )
        with mock.patch.object(upstream_mod, "has_unpushed_commits") as unpushed:
            decision = _decide_unpushed(
                state, options, branch_state=FakeBranchState(branch="topic")
            )

        assert decision.should_block is False
        assert decision.outcome == "skip"
        assert decision.matched_branch == protected_branch
        assert decision.match_kind == "upstream"
        unpushed.assert_not_called()


class TestUnpushedSnapshotBinding:
    """The protected-upstream skip is bound to the snapshot's own branch."""

    @staticmethod
    def _state(*, branch: str | None) -> state_mod.HookState:
        """Return state whose configured upstream ``main`` is protected."""
        return state_mod.HookState(
            git_facts=GitFacts(
                primary_remote="origin",
                upstream_ref="origin/main",
                pr_base_local_ref=None,
                local_base_commit=None,
                three_way_merge_is_configured=False,
                upstream_remote="origin",
                upstream_branch="main",
                upstream_merge_ref="refs/heads/main",
                branch=branch,
            )
        )

    @staticmethod
    def _decide_while_checked_out(
        branch: str | None,
    ) -> tuple[pipeline_mod.BranchStateGateDecision, _FakeUpstreamSource]:
        """Decide the gate on ``topic`` from a snapshot collected on ``branch``."""
        source = _FakeUpstreamSource(_comparison("origin", "main", ahead=True))
        decision = _decide_unpushed(
            TestUnpushedSnapshotBinding._state(branch=branch),
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            source,
            branch_state=FakeBranchState(branch="topic"),
        )
        return decision, source

    def test_a_snapshot_from_another_branch_is_not_applied(self) -> None:
        """A stale snapshot cannot lend ``topic`` its protection.

        The snapshot describes a checkout of ``release`` whose upstream is the
        protected ``main``, while ``topic`` is checked out now. Applying that
        protection would leave ``topic``'s unpushed commits unchecked, so the
        gate compares instead of skipping.
        """
        decision, source = self._decide_while_checked_out("release")

        assert source.calls == 1, "the gate must compare rather than skip"
        assert decision.match_kind is None, (
            f"the stale upstream protection must not be applied: {decision!r}"
        )
        assert decision.outcome == "block"

    def test_a_snapshot_without_a_branch_is_not_applied(self) -> None:
        """An unattributed upstream cannot protect the checked-out branch."""
        decision, source = self._decide_while_checked_out(None)

        assert source.calls == 1, "the gate must compare rather than skip"
        assert decision.match_kind is None, (
            f"an unattributed upstream must not protect the branch: {decision!r}"
        )
        assert decision.outcome == "block"

    def test_a_snapshot_for_the_checked_out_branch_still_skips(self) -> None:
        """A snapshot that names ``topic`` still applies its protection."""
        decision, source = self._decide_while_checked_out("topic")

        assert source.calls == 0, "a bound protected upstream must skip the gate"
        assert decision.outcome == "skip"
        assert decision.matched_branch == "main"
        assert decision.match_kind == "upstream"


class TestUnpushedUpstreamOptOut:
    """Opting out of upstream refresh can only use a local ref."""

    def test_url_upstream_with_refresh_disabled_passes(self) -> None:
        """A URL upstream has no local ref, so the gate fails open."""
        observer = _RecordingObserver()
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
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            decision = _decide_unpushed(state, options)
        observation_mod.record_gate_decision(observer, decision)

        fetch.assert_not_called()
        ahead.assert_not_called()
        assert decision.should_block is False
        assert decision.outcome == "pass"
        assert decision.error is not None
        assert decision.error.message == (
            "upstream refresh is disabled and the upstream is not a local ref"
        )
        assert any(
            "could not verify the upstream" in warning for warning in observer.warnings
        ), f"expected the gate warning, got {observer.warnings!r}"


def _comparison(  # noqa: PLR0913 - mirrors the value object it builds
    remote: str,
    branch: str,
    *,
    names: tuple[str, ...] | None = (),
    ahead: bool | None = None,
    error: HookError | None = None,
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
            label = formatting_mod.upstream_label(comparison)

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
            decision = branch_gates_mod.unpushed_commits_gate(refresh)

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
            decision = branch_gates_mod.unpushed_commits_gate(refresh)

        fetch.assert_not_called()
        ahead.assert_not_called()
        assert decision.should_block is False
        assert decision.outcome == "pass"

    def test_the_gate_never_calls_the_port(self) -> None:
        """The decision takes a value, so it cannot trigger transport work."""
        source = _FakeUpstreamSource(
            _comparison("origin", "feature", names=("origin",), ahead=True)
        )
        decision = branch_gates_mod.unpushed_commits_gate(
            _comparison("origin", "feature")
        )

        assert source.calls == 0, "constructing the decision must not read the port"
        assert decision.should_block is False

    def test_emits_the_block_from_the_prepared_comparison(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The emitter renders the same comparison the gate decided from."""
        formatting_mod.emit_unpushed_block(
            _comparison("origin", "feature", names=("origin",), ahead=True)
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload["decision"] == "block"
        assert "origin/feature" in payload["reason"]

    def test_a_malformed_credential_url_reaches_no_payload(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unrenderable URL is replaced by the branch name alone."""
        formatting_mod.emit_unpushed_block(
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
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            refresh = _refresh_unpushed(
                self._state("origin", "feature", "origin/feature"),
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                source,
            )
            decision = branch_gates_mod.unpushed_commits_gate(refresh)
            assert isinstance(refresh, upstream_port_mod.UpstreamComparison), (
                "the port must supply the comparison the gate decides from"
            )
            formatting_mod.emit_unpushed_block(refresh)

        fetch.assert_not_called()
        ahead.assert_not_called()
        assert source.calls == 1
        assert decision.should_block is True
        payload = json.loads(capsys.readouterr().out)
        assert "origin/feature" in payload["reason"]

    def test_unavailable_comparison_passes_and_is_reported(self) -> None:
        """An unverifiable upstream passes with the port's error."""
        observer = _RecordingObserver()
        source = _FakeUpstreamSource(
            _comparison(
                "origin",
                "feature",
                names=("origin",),
                ahead=None,
                error=HookError(
                    "fetch_upstream_head",
                    "git fetch origin feature failed: offline",
                ),
            )
        )
        decision = _decide_unpushed(
            self._state("origin", "feature", "origin/feature"),
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            source,
        )
        observation_mod.record_gate_decision(observer, decision)

        assert decision.should_block is False
        assert decision.outcome == "pass"
        assert decision.error is not None
        assert decision.error.message == "git fetch origin feature failed: offline"
        assert any(
            "could not verify the upstream origin/feature" in warning
            for warning in observer.warnings
        ), f"expected the port error warning, got {observer.warnings!r}"

    def test_local_upstream_comparison_passes(self) -> None:
        """A local upstream comparison passes when nothing is ahead."""
        source = _FakeUpstreamSource(_comparison(".", "integration", ahead=False))
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

    def test_repository_port_imports_no_adapter_or_transport_module(self) -> None:
        """The repository port depends on no Git, subprocess, or adapter."""
        banned = {
            "post_turn_quality_stop_hook.git",
            "post_turn_quality_stop_hook.repository_git",
            "post_turn_quality_stop_hook.process",
            "post_turn_quality_stop_hook.redaction",
            "subprocess",
        }

        assert not self._module_imports(repository_port_mod) & banned, (
            "the repository port must stay free of the adapter it abstracts"
        )

    def test_the_gate_module_imports_ports_but_no_adapter(self) -> None:
        """The decision path imports no Git, GitHub, driver, or formatting unit."""
        imported = self._module_imports(branch_gates_mod)
        banned = {
            "post_turn_quality_stop_hook.git",
            "post_turn_quality_stop_hook.github",
            "post_turn_quality_stop_hook.driver",
            "post_turn_quality_stop_hook.formatting",
            "post_turn_quality_stop_hook.templates",
            "post_turn_quality_stop_hook.upstream",
            "subprocess",
        }

        assert not imported & banned, (
            f"the gate module must import only ports, but reached {imported & banned!r}"
        )
        assert "post_turn_quality_stop_hook.branch_state_port" in imported
        assert "post_turn_quality_stop_hook.repository_port" in imported
        assert "post_turn_quality_stop_hook.upstream_label" in imported

    def test_the_composition_root_is_where_the_adapter_is_imported(self) -> None:
        """The entry point, not the pipeline, builds the Git adapter."""
        imported = self._module_imports(hook_mod)

        assert "post_turn_quality_stop_hook.upstream" in imported

    def test_only_the_refresh_command_reads_the_port(self) -> None:
        """The decision never calls the port; only the refresh step does."""
        assert self._functions_calling(branch_gates_mod, "refresh_comparison") == {
            "refresh_upstream_head"
        }


class TestTargetEnumerationLogging:
    """Target read failures remain visible at the quality execution boundary."""

    def test_enumeration_failure_warns_once(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A read error is reported once through the observer, not stdout."""
        error = "could not read /fake/repo/Makefile: permission denied"
        observer = _RecordingObserver()
        state = state_mod.HookState(changed_files=("app.py",))
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=driver,
            max_out=12000,
            enumerate_targets=lambda _repo, _driver: enumerated(
                HookError("get_make_targets", error)
            ),
            run_commands=lambda _repo, _request, _max_out: pytest.fail(
                "no command should run after an enumeration failure"
            ),
            observer=observer,
        )
        result = quality_mod.evaluate_changes(
            stage,
            quality_mod.select_categories(state.changed_files),
        )

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
        assert observer.warnings == [f"Could not enumerate build targets: {error}"], (
            "expected exactly one enumeration warning through the observer"
        )
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
            result = pipeline_mod.run_quality_checks_if_needed(
                state, REPO, options, observer=NullObserver(_no_warnings)
            )

        assert result is None, f"expected no blocking result but got {result!r}"
        select.assert_not_called()
        assert capsys.readouterr().out == "", "expected no output"

    def test_returns_enumeration_failure_report(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The quality stage returns the blocking report without printing it."""
        error = "could not read /fake/repo/Makefile: permission denied"
        observer = _RecordingObserver()
        state = state_mod.HookState(changed_files=("app.py",))
        options = state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        with (
            mock.patch.object(pipeline_mod, "select_build_driver", return_value=driver),
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value=enumerated(HookError("get_make_targets", error)),
            ),
        ):
            result = pipeline_mod.run_quality_checks_if_needed(
                state, REPO, options, observer=observer
            )

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
        assert observer.warnings == [f"Could not enumerate build targets: {error}"], (
            "expected the quality unit to report the warning once"
        )
        assert capsys.readouterr().out == "", "expected the stage to stay silent"


class TestPrRebaseCheck:
    """Tests for the pull-request rebase gate.

    Every answer comes from a prepared port, so these tests run no Git,
    GitHub, subprocess, or template renderer. ``pr_rebase_check`` renders the
    payload the gate decided on, which is the seam the payload assertions use.
    """

    @staticmethod
    def _state(*, three_way: bool = False) -> state_mod.HookState:
        """Return hook state whose facts name the primary remote."""
        return state_mod.HookState(
            git_facts=GitFacts(
                primary_remote="origin",
                upstream_ref="origin/feature",
                pr_base_local_ref=None,
                local_base_commit="base",
                three_way_merge_is_configured=three_way,
            )
        )

    @staticmethod
    def _ports(
        *,
        branch_state: FakeBranchState | None = None,
        available: frozenset[str] | HookError | None = None,
    ) -> branch_state_mod.BranchGatePorts:
        """Return ports that locate one open pull request."""
        return gate_ports(
            branch_state or FakeBranchState(),
            summary=pull_request(),
            available=available,
        )

    @pytest.mark.parametrize("target_error", [None, "Makefile permission denied"])
    def test_pr_base_ahead_blocks(
        self,
        capsys: pytest.CaptureFixture[str],
        target_error: str | None,
    ) -> None:
        """Ahead PR base emits the rebase template as a block payload."""
        observer = _RecordingObserver()
        available: frozenset[str] | HookError = (
            HookError("get_make_targets", target_error)
            if target_error
            else frozenset({"check-fmt", "lint", "typecheck"})
        )
        branch_state = FakeBranchState(ancestor=True)

        rc = pipeline_mod.pr_rebase_check(
            REPO,
            self._state(three_way=True),
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            observer=observer,
            ports=self._ports(branch_state=branch_state, available=available),
        )

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["decision"] == "block"
        assert "Please rebase this branch onto `origin/main`" in payload["reason"]
        assert "using the `rebase` skill" in payload["reason"]
        assert ("`make typecheck`" in payload["reason"]) is (target_error is None)
        expected = (
            [f"Could not enumerate build targets: {target_error}"]
            if target_error
            else []
        )
        assert observer.warnings == expected
        assert "ensure_remote_branch_ref" in branch_state.calls, (
            "the gate must refresh the pull-request base before comparing"
        )

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
        lookup = FakePullRequestLookup(pull_request())
        ports = branch_state_mod.BranchGatePorts(
            branch_state=FakeBranchState(),
            lookup_pull_request=lookup,
            enumerate_build_targets=FakeTargetEnumerator(frozenset()),
        )

        rc = pipeline_mod.pr_rebase_check(
            REPO,
            state,
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            observer=NullObserver(_no_warnings),
            ports=ports,
        )

        assert rc == 0
        assert lookup.calls == [], "a missing remote must not reach the lookup"
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize(
        ("branch_state", "reason"),
        [
            (
                FakeBranchState(fetch_ok=False),
                "could not fetch the pull request base",
            ),
            (
                FakeBranchState(merged=None, merge_error=HookError("m", "failed")),
                "could not compute the pull request merge base",
            ),
            (
                FakeBranchState(ancestor=None, ancestor_error=HookError("a", "failed")),
                "could not compare the pull request base",
            ),
        ],
        ids=["fetch-failed", "merge-base-failed", "ancestry-failed"],
    )
    def test_an_undetermined_base_is_a_skip_not_a_pass(
        self,
        capsys: pytest.CaptureFixture[str],
        branch_state: FakeBranchState,
        reason: str,
    ) -> None:
        """A failed step leaves the gate undetermined, never a silent pass.

        The durable log records a skip so a failed fetch, merge base, or
        ancestry comparison is never mistaken for a verifying pass. The gate
        still prints nothing and returns ``0``.
        """
        observer = _RecordingObserver()

        rc = pipeline_mod.pr_rebase_check(
            REPO,
            self._state(),
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            observer=observer,
            ports=self._ports(branch_state=branch_state),
        )

        assert rc == 0
        assert observer.decisions == [("pr_rebase", "skip", reason, None)], (
            "expected the undetermined gate to be recorded as a skip but got"
            f" {observer.decisions!r}"
        )
        assert observer.blocks == [], "an undetermined gate must not mark a block"
        assert capsys.readouterr().out == "", "an undetermined gate prints nothing"

    def test_the_typecheck_hint_follows_the_declared_targets(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The hint is shown only for a declared ``typecheck`` target.

        A driver that declares other quality targets but no ``typecheck``
        target omits the hint without reporting a target-read failure, so the
        hint decision and the warning stay derived from one enumeration.
        """
        observer = _RecordingObserver()

        rc = pipeline_mod.pr_rebase_check(
            REPO,
            self._state(),
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            observer=observer,
            ports=self._ports(
                branch_state=FakeBranchState(ancestor=True),
                available=frozenset({"check-fmt", "lint"}),
            ),
        )

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["decision"] == "block"
        assert "`make typecheck`" not in payload["reason"]
        assert observer.warnings == []

    def test_the_base_is_compared_with_the_captured_commit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The merge base uses the commit captured when the lookup began.

        ``git merge-base`` defaults to live ``HEAD``. Pinning it to the
        captured commit keeps the comparison about the branch the pull
        request was looked up for.
        """
        observer = _RecordingObserver()
        branch_state = FakeBranchState(commit="captured")

        rc = pipeline_mod.pr_rebase_check(
            REPO,
            self._state(),
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            observer=observer,
            ports=self._ports(branch_state=branch_state),
        )

        assert rc == 0
        assert "merge_base:captured" in branch_state.calls, (
            f"the captured commit must pin the merge base, got {branch_state.calls!r}"
        )
        assert "merge_base:HEAD" not in branch_state.calls
        assert observer.decisions == [
            ("pr_rebase", "pass", "pull request base is not ahead", None)
        ], f"expected the passing comparison to be recorded, got {observer.decisions!r}"
        assert capsys.readouterr().out == "", "a passing gate prints nothing"

    @pytest.mark.parametrize(
        ("branches", "commits"),
        [
            (["feature", "feature", "other"], None),
            (None, ["captured", "moved", "moved"]),
        ],
        ids=["branch-moved", "commit-moved"],
    )
    def test_a_checkout_that_moves_is_a_skip_not_a_block(
        self,
        capsys: pytest.CaptureFixture[str],
        branches: list[str | None] | None,
        commits: list[str | None] | None,
    ) -> None:
        """A checkout that moves during the check is undetermined.

        The ancestry answer would otherwise report a rebase for a branch that
        is no longer checked out, so the gate skips with its fixed reason. The
        branch case moves only between the commit comparison and the final
        branch re-read, so the guard's second read is what fails it closed.
        """
        observer = _RecordingObserver()
        branch_state = FakeBranchState(
            commit="captured", ancestor=True, branches=branches, commits=commits
        )

        rc = pipeline_mod.pr_rebase_check(
            REPO,
            self._state(),
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            observer=observer,
            ports=self._ports(branch_state=branch_state),
        )

        assert rc == 0
        assert observer.decisions == [
            (
                "pr_rebase",
                "skip",
                "the checkout changed during the pull request check",
                None,
            )
        ], f"a moved checkout must be recorded as a skip, got {observer.decisions!r}"
        assert capsys.readouterr().out == "", "a moved checkout prints nothing"

    def test_an_unreadable_commit_is_a_skip(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A lookup that cannot bind to a commit leaves the gate undetermined."""
        observer = _RecordingObserver()

        rc = pipeline_mod.pr_rebase_check(
            REPO,
            self._state(),
            state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            observer=observer,
            ports=self._ports(
                branch_state=FakeBranchState(
                    commit=None,
                    commit_error=HookError("head_commit", "HEAD is unreadable"),
                )
            ),
        )

        assert rc == 0
        assert observer.decisions == [
            ("pr_rebase", "skip", "could not determine the current commit", None)
        ], f"an unreadable commit must skip, got {observer.decisions!r}"
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
        field_name = "ok"
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(state, field_name, False)

    def test_collection_fields_reject_mutation(self) -> None:
        """Snapshots expose only immutable collection types."""
        state = state_mod.HookState(
            changed_files=("src/a.py",),
            categories=frozenset({"code"}),
            targets_requested=("check-fmt",),
            commands=(_command(),),
        )
        mutations = (
            ("categories", "add", "markdown"),
            ("changed_files", "append", "src/b.py"),
            ("commands", "append", _command()),
        )

        for field, method, value in mutations:
            with pytest.raises(AttributeError):
                getattr(getattr(state, field), method)(value)

    def test_recorded_commands_remain_frozen(self) -> None:
        """A recorded command cannot change the snapshot's blocked result."""
        evaluation = state_mod.QualityEvaluation(
            categories=frozenset({"code"}),
            commands=(_command(exit_code=1),),
        )

        field_name = "exit_code"
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(evaluation.commands[0], field_name, 0)
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
# weave: run 'weave explain tests/test_pipeline.py' for per-hunk detail, 'weave check' to verify your resolution
