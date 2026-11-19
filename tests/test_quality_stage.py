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
from quality_support import GIT_FACTS
from quality_support import command as _command

from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import execution as exec_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import quality as quality_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.errors import HookError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

REPO = Path("/fake/repo")


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
        with mock.patch.object(
            quality_mod,
            "get_build_targets",
            return_value={"check-fmt", "markdownlint"},
        ):
            targets = quality_mod.enumerate_quality_targets(
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
            categories=frozenset({"code"}),
            targets_requested=("check-fmt", "lint", "typecheck"),
        )
        with mock.patch.object(
            quality_mod, "get_build_targets", return_value={"build"}
        ):
            targets = quality_mod.enumerate_quality_targets(
                REPO, self.driver, selection
            )

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
        with mock.patch.object(quality_mod, "get_build_targets", return_value=error):
            targets = quality_mod.enumerate_quality_targets(
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
            quality_mod,
            "run_build_targets",
            side_effect=[code_result, markdown_result],
        ) as run_targets:
            commands = quality_mod.execute_quality_targets(
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
        with mock.patch.object(quality_mod, "run_build_targets") as run_targets:
            commands = quality_mod.execute_quality_targets(
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
        mock.patch.object(quality_mod, "get_build_targets", return_value=targets),
        mock.patch.object(
            quality_mod, "run_build_targets", side_effect=run_results
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
        records = [r for r in caplog.records if r.name == quality_mod.__name__]
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
