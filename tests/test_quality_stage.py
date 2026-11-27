"""Exercise the quality stage's immutable units and pipeline integration.

The tests cover category detection, target selection, command execution, and
the orchestrator's use of the immutable quality records. Branch-state gates and
the PR-rebase check remain covered by ``test_pipeline.py``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import typing as typ
from pathlib import Path
from unittest import mock

import pytest
from branch_support import gate_ports
from hypothesis import given
from hypothesis import strategies as st
from observer_support import RecordingObserver
from observer_support import no_warnings as _no_warnings
from quality_support import GIT_FACTS, UnusedRepositoryFacts, enumerated
from quality_support import command as _command

from post_turn_quality_stop_hook import diagnostics as warn_mod
from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import execution as exec_mod
from post_turn_quality_stop_hook import observation as observation_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import quality as quality_mod
from post_turn_quality_stop_hook import quality_stage as quality_stage_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook import upstream as upstream_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.observer import NullObserver

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from post_turn_quality_stop_hook.git_facts import GitFacts

REPO = Path("/fake/repo")


def _unused_upstream_source(
    _repo: Path,
    _facts: GitFacts | None,
    _observer: object,
) -> upstream_mod.GitUpstreamHeadSource:
    """Build a port that these paths must never consult."""
    return upstream_mod.GitUpstreamHeadSource(
        _repo,
        _facts,
        refresh_upstream=True,
        fetch_timeout_seconds=10.0,
        on_warning=lambda _message: None,
    )


def _enter_patchers(
    patches: cabc.Iterable[contextlib.AbstractContextManager[object]],
) -> contextlib.ExitStack:
    """Enter every patcher and return the stack that owns them.

    A failure part way through unwinds the patchers already entered, so one
    bad patch target cannot leak into later tests.
    """
    with contextlib.ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        return stack.pop_all()


_DRIVER = driver_mod.BuildDriver("make", "make", "Makefile")


def _no_targets(
    _repo: Path, _driver: driver_mod.BuildDriver
) -> driver_mod.TargetEnumeration:
    """Return a driver with no declared targets."""
    return enumerated(set())


def _never_runs(
    _repo: Path, _request: exec_mod.BuildTargetRequest, _max_out: int
) -> exec_mod.CommandResult:
    """Fail if a test path unexpectedly runs a build command."""
    pytest.fail("run_commands must not be called")


def _stage(
    *,
    enumerate_targets: quality_stage_mod.EnumerateTargets = _no_targets,
    run_commands: cabc.Callable[
        [Path, exec_mod.BuildTargetRequest, int], exec_mod.CommandResult
    ] = _never_runs,
) -> quality_mod.QualityStage:
    """Build a quality stage whose ports are supplied explicitly."""
    return quality_mod.QualityStage(
        repo=REPO,
        driver=_DRIVER,
        max_out=12000,
        enumerate_targets=enumerate_targets,
        run_commands=run_commands,
    )


class TestQualityUnits:
    """Tests for the split category, target and execution units."""

    driver = driver_mod.BuildDriver("make", "make", "Makefile")

    def test_select_categories_maps_files_to_requested_targets(self) -> None:
        """Detected categories expand into their declared target lists."""
        selection = quality_mod.select_categories([
            "src/app.py",
            "docs/users-guide.md",
        ])

        assert selection.categories == frozenset({"code", "markdown"}), (
            f"expected code and markdown but got {selection.categories!r}"
        )
        assert selection.targets_requested == (
            "check-fmt",
            "lint",
            "typecheck",
            "markdownlint",
            "nixie",
        ), f"expected both target groups but got {selection.targets_requested!r}"

    def test_select_categories_ignores_unrecognized_files(self) -> None:
        """Files without a recognized extension request no targets."""
        selection = quality_mod.select_categories(["LICENSE"])

        assert selection.categories == frozenset(), (
            f"expected no categories but got {selection.categories!r}"
        )
        assert selection.targets_requested == (), (
            f"expected no requested targets but got {selection.targets_requested!r}"
        )

    def test_enumerate_quality_targets_separates_present_and_missing(self) -> None:
        """Requested targets are split into present, skipped and grouped lists."""
        selection = state_mod.CategorySelection(
            categories=frozenset({"code", "markdown"}),
            targets_requested=(
                "check-fmt",
                "lint",
                "typecheck",
                "markdownlint",
                "nixie",
            ),
        )
        stage = _stage(
            enumerate_targets=lambda _repo, _driver: enumerated({
                "check-fmt",
                "markdownlint",
            })
        )
        targets = quality_mod.enumerate_quality_targets(stage, selection)

        # The selection now also carries the driver's full enumeration. The
        # review of the decision-log branch asked that the record report every
        # declared target, not only the requested subset, so the expected value
        # includes ``targets_available``; ``targets_present`` keeps its old
        # meaning for selection and execution.
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
            targets_available=("check-fmt", "markdownlint"),
        ), f"unexpected target selection {targets!r}"

    def test_enumerate_quality_targets_skips_all_missing_targets(self) -> None:
        """A driver with no matching targets succeeds with everything skipped."""
        selection = state_mod.CategorySelection(
            categories=frozenset({"code"}),
            targets_requested=("check-fmt", "lint", "typecheck"),
        )
        stage = _stage(enumerate_targets=lambda _repo, _driver: enumerated({"build"}))
        targets = quality_mod.enumerate_quality_targets(stage, selection)

        match targets:
            case state_mod.TargetSelection():
                pass
            case _:
                pytest.fail(f"expected a target selection but got {targets!r}")
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
            categories=frozenset({"code"}),
            targets_requested=("check-fmt",),
        )
        stage = _stage(enumerate_targets=lambda _repo, _driver: enumerated(error))
        targets = quality_mod.enumerate_quality_targets(stage, selection)

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
        run_targets = mock.Mock(side_effect=[code_result, markdown_result])
        stage = _stage(run_commands=run_targets)
        commands = quality_mod.execute_quality_targets(stage, selection)

        assert commands == (code_result, markdown_result), (
            f"expected both command results in order but got {commands!r}"
        )
        kinds = [call.args[1].kind for call in run_targets.call_args_list]
        assert kinds == ["code", "markdown"], (
            f"expected code then markdown groups but got {kinds!r}"
        )

    def test_execute_quality_targets_skips_empty_groups(self) -> None:
        """Groups with no present targets do not invoke the build driver."""
        run_targets = mock.Mock()
        stage = _stage(run_commands=run_targets)
        commands = quality_mod.execute_quality_targets(
            stage, state_mod.TargetSelection()
        )

        assert commands == (), f"expected no command results but got {commands!r}"
        run_targets.assert_not_called()


class TestTargetsRun:
    """The record's run list names only targets whose executable started.

    The ``_targets_run`` rule is exercised directly, because the difference it
    makes is a target that never launched appearing in the decision log.
    """

    def test_a_group_that_never_launched_is_not_reported_as_run(self) -> None:
        """A missing executable contributes no targets to ``targets_run``."""
        targets = state_mod.TargetSelection(
            code_targets=("check-fmt", "lint", "typecheck"),
        )
        unstarted = exec_mod.CommandResult(
            kind="code",
            cmd="make --no-print-directory check-fmt lint typecheck",
            exit_code=127,
            stdout="",
            stderr="make not found on PATH: No such file or directory",
            launched=False,
        )

        assert observation_mod.quality_targets_run(targets, (unstarted,)) == [], (
            "a group whose executable never started must not be reported as run"
        )

    def test_a_launched_child_that_exits_127_is_reported_as_run(self) -> None:
        """A started command that returns 127 itself still ran."""
        targets = state_mod.TargetSelection(
            code_targets=("check-fmt",),
            markdown_targets=("markdownlint",),
        )
        code_127 = exec_mod.CommandResult(
            kind="code",
            cmd="make --no-print-directory check-fmt",
            exit_code=127,
            stdout="",
            stderr="recipe failed",
        )

        assert observation_mod.quality_targets_run(targets, (code_127,)) == [
            "check-fmt"
        ], "a child that started and exited 127 must count as having run"

    def test_targets_available_reports_the_full_enumeration(self) -> None:
        """A declared target the categories did not select is still available.

        A code-only change selects no ``release`` target, but the driver
        declares one. The record must report the driver's full enumeration
        rather than the requested subset.
        """
        observer = RecordingObserver()
        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=_DRIVER,
            max_out=12000,
            enumerate_targets=lambda _repo, _driver: enumerated({
                "check-fmt",
                "lint",
                "typecheck",
                "release",
            }),
            run_commands=lambda _repo, _request, _max_out: _command(),
            observer=observer,
        )

        quality_mod.evaluate_changes(
            stage, quality_mod.select_categories(("src/app.py",))
        )

        assert observer.selections, "the evaluation must publish its selection"
        # The final selection is published once execution settles; earlier
        # events carry only what was known before enumeration.
        fields = observer.selections[-1]
        assert fields["targets_available"] == [
            "check-fmt",
            "lint",
            "release",
            "typecheck",
        ], f"expected the full enumeration but got {fields['targets_available']!r}"

    def test_the_driver_and_request_list_are_published_before_enumeration(
        self,
    ) -> None:
        """An interrupted enumeration still records what the run selected."""
        observer = RecordingObserver()
        published_before: list[int] = []

        def _enumerate(
            _repo: Path, _driver: driver_mod.BuildDriver
        ) -> driver_mod.TargetEnumeration:
            """Record how much had been published before enumeration began."""
            published_before.append(len(observer.selections))
            return enumerated({"check-fmt", "lint", "typecheck"})

        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=_DRIVER,
            max_out=12000,
            enumerate_targets=_enumerate,
            run_commands=lambda _repo, _request, _max_out: _command(),
            observer=observer,
        )
        selection = quality_mod.select_categories(("src/app.py",))

        quality_mod.evaluate_changes(stage, selection)

        assert published_before == [1], (
            "the selection must be published before the blocking enumeration"
        )
        early = observer.selections[0]
        assert early["build_driver"] == _DRIVER.name
        assert early["targets_requested"] == list(selection.targets_requested)
        assert early["targets_available"] == [], (
            "nothing is available until enumeration has answered"
        )

    def test_a_completed_group_is_published_before_the_next_starts(self) -> None:
        """A later interruption keeps the targets of the group that finished."""
        observer = RecordingObserver()
        before_markdown: list[dict[str, object]] = []

        def _run(
            _repo: Path, request: exec_mod.BuildTargetRequest, _max_out: int
        ) -> exec_mod.CommandResult:
            """Record the published facts when the Markdown group starts."""
            if request.kind == "markdown":
                before_markdown.append(observer.selections[-1])
            return _command(kind=request.kind)

        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=_DRIVER,
            max_out=12000,
            enumerate_targets=lambda _repo, _driver: enumerated({
                "check-fmt",
                "lint",
                "typecheck",
                "markdownlint",
            }),
            run_commands=_run,
            observer=observer,
        )

        quality_mod.evaluate_changes(
            stage,
            quality_mod.select_categories(("src/app.py", "docs/README.md")),
        )

        assert before_markdown, "the Markdown group must have started"
        assert before_markdown[0]["targets_run"] == [
            "check-fmt",
            "lint",
            "typecheck",
        ], (
            "the completed code group's targets must be published before the"
            f" next group starts, got {before_markdown[0]['targets_run']!r}"
        )

    def test_a_group_that_never_launched_publishes_no_run_targets(self) -> None:
        """A launch failure adds no target to the published run list."""
        observer = RecordingObserver()
        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=_DRIVER,
            max_out=12000,
            enumerate_targets=lambda _repo, _driver: enumerated({"check-fmt"}),
            run_commands=lambda _repo, _request, _max_out: _command(
                exit_code=127, launched=False
            ),
            observer=observer,
        )
        selection = state_mod.TargetSelection(
            targets_requested=("check-fmt",),
            code_targets=("check-fmt",),
        )

        quality_mod.execute_quality_targets(stage, selection)

        assert observer.selections == [], (
            "a group whose executable never started must publish no run targets"
        )


class TestPrepareRunStopChecks:
    """Tests for immutable preparation outcomes."""

    def test_missing_working_directory_returns_stop(self, tmp_path: Path) -> None:
        """A missing working directory is a quiet non-repository stop."""
        start_cwd = tmp_path / "missing"
        with (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(
                pipeline_mod,
                "repo_root",
                return_value=(
                    None,
                    HookError("repo_root", "No such file or directory"),
                ),
            ),
        ):
            preparation = pipeline_mod.prepare_run_stop_checks(
                start_cwd,
                "origin/main",
                always_fetch=False,
                config=Config(),
                observer=NullObserver(_no_warnings),
            )

        match preparation:
            case state_mod.PreparationStop():
                pass
            case _:
                pytest.fail(f"expected a quiet stop but got {preparation!r}")
        assert preparation.state.error is None, (
            f"expected no reported error but got {preparation.state.error!r}"
        )

    def test_non_repository_directory_returns_stop(self, tmp_path: Path) -> None:
        """Git's not-a-repository diagnostic is also a quiet stop."""
        with (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(
                pipeline_mod,
                "repo_root",
                return_value=(
                    None,
                    HookError("repo_root", "fatal: not a git repository"),
                ),
            ),
        ):
            preparation = pipeline_mod.prepare_run_stop_checks(
                tmp_path,
                "origin/main",
                always_fetch=False,
                config=Config(),
                observer=NullObserver(_no_warnings),
            )

        match preparation:
            case state_mod.PreparationStop():
                pass
            case _:
                pytest.fail(f"expected a quiet stop but got {preparation!r}")
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
                observer=NullObserver(_no_warnings),
            )

        match preparation:
            case state_mod.PreparationBlock():
                pass
            case _:
                pytest.fail(f"expected a block outcome but got {preparation!r}")
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
                return_value=(None, HookError("changed_files", "git diff failed")),
            ),
        )
        with _enter_patchers(patches):
            preparation = pipeline_mod.prepare_run_stop_checks(
                REPO,
                "origin/main",
                always_fetch=options.always_fetch,
                config=options.config,
                observer=NullObserver(_no_warnings),
            )

        match preparation:
            case state_mod.PreparationBlock():
                pass
            case _:
                pytest.fail(f"expected a block outcome but got {preparation!r}")
        assert preparation.error.operation == "changed_files", (
            f"expected the changed_files operation but got "
            f"{preparation.error.operation!r}"
        )
        assert preparation.repo == REPO, (
            f"expected the resolved repository but got {preparation.repo!r}"
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
            rc = pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                options,
                _unused_upstream_source,
                branch_gate_ports=gate_ports(),
                repository_facts_source=UnusedRepositoryFacts(),
                warn=_no_warnings,
            )

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

    def test_base_ref_failure_blocks_without_running_commands(self) -> None:
        """A failed base-ref fetch blocks and records the fetch state."""
        merge_base = mock.Mock()
        patches = (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "collect_git_facts", return_value=GIT_FACTS
            ),
            mock.patch.object(
                pipeline_mod,
                "ensure_base_ref",
                return_value=(
                    False,
                    HookError("ensure_base_ref", "fetch failed"),
                    False,
                ),
            ),
            mock.patch.object(pipeline_mod, "merge_base", merge_base),
        )

        with _enter_patchers(patches):
            preparation = pipeline_mod.prepare_run_stop_checks(
                REPO,
                "origin/main",
                always_fetch=False,
                config=Config(),
                observer=NullObserver(_no_warnings),
            )

        match preparation:
            case state_mod.PreparationBlock():
                pass
            case _:
                pytest.fail(f"expected a block outcome but got {preparation!r}")
        assert preparation.error.operation == "ensure_base_ref", (
            f"expected ensure_base_ref but got {preparation.error.operation!r}"
        )
        assert str(preparation.error) == "fetch failed", (
            f"expected the fetch failure but got {str(preparation.error)!r}"
        )
        assert preparation.state.ok is False, (
            f"expected an unsuccessful snapshot but got {preparation.state.ok!r}"
        )
        assert preparation.state.fetched is False, (
            f"expected no completed fetch but got {preparation.state.fetched!r}"
        )
        assert preparation.repo == REPO, (
            f"expected the resolved repository but got {preparation.repo!r}"
        )
        merge_base.assert_not_called()

    def test_merge_base_failure_retains_the_fetch_fact(self) -> None:
        """A merge-base failure blocks before enumeration and keeps fetch state."""
        changed_files = mock.Mock()
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
                pipeline_mod,
                "merge_base",
                return_value=(None, HookError("merge_base", "no merge base")),
            ),
            mock.patch.object(pipeline_mod, "changed_files", changed_files),
        )

        with _enter_patchers(patches):
            preparation = pipeline_mod.prepare_run_stop_checks(
                REPO,
                "origin/main",
                always_fetch=False,
                config=Config(),
                observer=NullObserver(_no_warnings),
            )

        match preparation:
            case state_mod.PreparationBlock():
                pass
            case _:
                pytest.fail(f"expected a block outcome but got {preparation!r}")
        assert preparation.error.operation == "merge_base", (
            f"expected merge_base but got {preparation.error.operation!r}"
        )
        assert str(preparation.error) == "no merge base", (
            f"expected the merge-base failure but got {str(preparation.error)!r}"
        )
        assert preparation.state.fetched is True, (
            f"expected the completed fetch to be retained but got "
            f"{preparation.state.fetched!r}"
        )
        changed_files.assert_not_called()


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
    warn: cabc.Callable[[str], None] = _no_warnings,
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
        mock.patch.object(
            pipeline_mod, "get_build_targets", return_value=enumerated(targets)
        ),
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
            _unused_upstream_source,
            branch_gate_ports=gate_ports(),
            repository_facts_source=UnusedRepositoryFacts(),
            warn=warn,
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
        assert "Targets run: check-fmt lint typecheck markdownlint nixie" in reason, (
            f"expected the executed targets in the reason but got {reason!r}"
        )
        assert (
            "Command failed (exit 1): "
            "make --no-print-directory check-fmt lint typecheck"
        ) in reason, f"expected the code failure in the reason but got {reason!r}"
        assert "make --no-print-directory markdownlint nixie" not in reason, (
            f"expected the passing Markdown group to be omitted but got {reason!r}"
        )
        result.uncommitted.assert_not_called()
        result.unpushed.assert_not_called()
        result.rebase.assert_not_called()

    def test_failed_code_build_reports_only_the_run_targets(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing target is reported as skipped, not as run."""
        targets = {"check-fmt", "lint", "typecheck", "markdownlint"}
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
                    cmd="make --no-print-directory markdownlint",
                ),
            ],
        )

        assert result.rc == 0, f"expected return 0 but got {result.rc!r}"
        reason = json.loads(capsys.readouterr().out)["reason"]
        assert "Targets run: check-fmt lint typecheck markdownlint" in reason, (
            f"expected only the present targets to run but got {reason!r}"
        )
        assert "Targets run: check-fmt lint typecheck markdownlint nixie" not in (
            reason
        ), f"expected nixie to be excluded from the run line but got {reason!r}"
        assert "Targets skipped (missing): nixie" in reason, (
            f"expected nixie to be reported as skipped but got {reason!r}"
        )

    def test_driver_selection_failure_returns_a_blocking_report(self) -> None:
        """A driver-selection error becomes a report without running checks."""
        error = HookError("select_build_driver", "netsuke not found")
        state = state_mod.HookState(changed_files=("app.py",))
        with (
            mock.patch.object(pipeline_mod, "select_build_driver", return_value=error),
            mock.patch.object(pipeline_mod, "evaluate_changes") as evaluate,
        ):
            report = pipeline_mod.run_quality_checks_if_needed(
                state,
                REPO,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                observer=NullObserver(_no_warnings),
            )

        assert isinstance(report, state_mod.HookState), (
            f"expected a blocking report but got {report!r}"
        )
        assert report.ok is False, (
            f"expected an unsuccessful report but got {report.ok!r}"
        )
        assert report.error == "netsuke not found", (
            f"expected the selection failure but got {report.error!r}"
        )
        evaluate.assert_not_called()

    def test_driver_selection_failure_blocks_before_branch_gates(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The orchestrator emits the driver-selection block and stops."""
        error = HookError("select_build_driver", "netsuke not found")
        passing = pipeline_mod.BranchStateGateDecision(gate="test", outcome="pass")
        with (
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
                pipeline_mod, "changed_files", return_value=(["app.py"], None)
            ),
            mock.patch.object(pipeline_mod, "select_build_driver", return_value=error),
            mock.patch.object(pipeline_mod, "evaluate_changes") as evaluate,
            mock.patch.object(
                pipeline_mod, "uncommitted_changes_gate", return_value=passing
            ) as uncommitted,
            mock.patch.object(
                pipeline_mod, "unpushed_commits_gate", return_value=passing
            ) as unpushed,
            mock.patch.object(pipeline_mod, "pr_rebase_check") as rebase,
        ):
            rc = pipeline_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                _unused_upstream_source,
                branch_gate_ports=gate_ports(),
                repository_facts_source=UnusedRepositoryFacts(),
                warn=_no_warnings,
            )

        payload = json.loads(capsys.readouterr().out)
        assert rc == 0, f"expected return 0 but got {rc!r}"
        assert payload["decision"] == "block", (
            f"expected a blocking decision but got {payload['decision']!r}"
        )
        assert "netsuke not found" in payload["reason"], (
            "expected the selection failure in the reason but got "
            f"{payload['reason']!r}"
        )
        evaluate.assert_not_called()
        uncommitted.assert_not_called()
        unpushed.assert_not_called()
        rebase.assert_not_called()

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
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An enumeration failure emits one warning and one block payload.

        The run composes a no-op observer, whose warning channel the
        composition root binds to the best-effort standard-error write. The
        warning therefore reaches standard error without a logger; standard
        output still carries only the decision payload.
        """
        error = HookError("get_make_targets", "permission denied")
        result = _run_stop_checks_with_quality(error, [], warn=warn_mod.warn)

        assert result.rc == 0, f"expected return 0 but got {result.rc!r}"
        captured = capsys.readouterr()
        message = "Could not enumerate build targets: permission denied"
        assert captured.err.count(message) == 1, (
            f"expected exactly one warning on stderr but got {captured.err!r}"
        )
        payload = json.loads(captured.out)
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


# ---------------------------------------------------------------------------
# QualityEvaluation properties

# ---------------------------------------------------------------------------


class TestQualityEvaluationProperties:
    """Hypothesis properties for the immutable quality result."""

    @given(
        st.lists(st.integers(min_value=-5, max_value=5)),
        st.booleans(),
    )
    def test_blocked_follows_errors_and_exit_codes(
        self, exit_codes: list[int], has_error: bool
    ) -> None:
        """The blocked flag is the error flag or any non-zero exit code."""
        evaluation = state_mod.QualityEvaluation(
            categories=frozenset({"code"}),
            commands=tuple(_command(exit_code=code) for code in exit_codes),
            error=HookError("test", "boom") if has_error else None,
        )

        expected = has_error or any(code != 0 for code in exit_codes)

        assert evaluation.blocked is expected

    @given(st.booleans())
    def test_empty_commands_block_only_with_an_error(self, has_error: bool) -> None:
        """No commands and no error never blocks."""
        evaluation = state_mod.QualityEvaluation(
            commands=(),
            error=HookError("test", "boom") if has_error else None,
        )

        assert evaluation.blocked is has_error

    @given(
        st.lists(
            st.sampled_from(["check-fmt", "lint", "typecheck"]),
            unique=True,
            max_size=3,
        ),
        st.lists(st.sampled_from(["markdownlint", "nixie"]), unique=True, max_size=2),
        st.lists(st.integers(min_value=0, max_value=1), min_size=1, max_size=5),
    )
    def test_execute_quality_targets_orders_code_before_markdown(
        self,
        code_targets: list[str],
        markdown_targets: list[str],
        exit_codes: list[int],
    ) -> None:
        """Every code command precedes every Markdown command."""
        selection = state_mod.TargetSelection(
            code_targets=tuple(code_targets),
            markdown_targets=tuple(markdown_targets),
        )
        codes = iter(exit_codes)
        driver = driver_mod.BuildDriver("make", "make", "Makefile")

        def fake_run(
            _repo: Path, request: exec_mod.BuildTargetRequest, _max_out: int
        ) -> exec_mod.CommandResult:
            """Build one command result for the requested target group."""
            return _command(
                kind=request.kind,
                exit_code=next(codes, 0),
                cmd=" ".join(request.targets),
            )

        stage = quality_mod.QualityStage(
            repo=REPO,
            driver=driver,
            max_out=12000,
            enumerate_targets=_no_targets,
            run_commands=fake_run,
        )
        commands = quality_mod.execute_quality_targets(stage, selection)

        kinds = [command.kind for command in commands]
        assert kinds == sorted(kinds, key=lambda kind: 0 if kind == "code" else 1)
