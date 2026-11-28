"""Exercise the quality stage's immutable units and pipeline integration.

The tests cover category detection, target selection, command execution, and
the orchestrator's use of the immutable quality records. Branch-state gates and
the PR-rebase check remain covered by ``test_pipeline.py``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import types
import typing as typ
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st
from quality_support import GIT_FACTS
from quality_support import command as _command

from post_turn_quality_stop_hook import disk_space as disk_mod
from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import execution as exec_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import quality as quality_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook import upstream as upstream_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.errors import HookError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from post_turn_quality_stop_hook.git_facts import GitFacts

REPO = Path("/fake/repo")


def _usage(free: int) -> types.SimpleNamespace:
    """Build the part of ``shutil.disk_usage``'s result the policy reads."""
    return types.SimpleNamespace(total=free, used=0, free=free)


#: Free space the shared autouse fixture reports: one tebibyte, far above any
#: sane threshold.
TESTING_FREE_BYTES = 1024**4


def _unused_upstream_source(
    _repo: Path, _facts: GitFacts | None
) -> upstream_mod.GitUpstreamHeadSource:
    """Build a port that these paths must never consult."""
    return upstream_mod.GitUpstreamHeadSource(
        _repo,
        _facts,
        refresh_upstream=True,
        fetch_timeout_seconds=10.0,
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


def _no_targets(_repo: Path, _driver: driver_mod.BuildDriver) -> set[str]:
    """Return a driver with no declared targets."""
    return set()


def _never_runs(
    _repo: Path, _request: exec_mod.BuildTargetRequest, _max_out: int
) -> exec_mod.CommandResult:
    """Fail if a test path unexpectedly runs a build command."""
    pytest.fail("run_commands must not be called")


def _stage(
    *,
    enumerate_targets: cabc.Callable[
        [Path, driver_mod.BuildDriver], set[str] | HookError
    ] = _no_targets,
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
            enumerate_targets=lambda _repo, _driver: {
                "check-fmt",
                "markdownlint",
            }
        )
        targets = quality_mod.enumerate_quality_targets(stage, selection)

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
        stage = _stage(enumerate_targets=lambda _repo, _driver: {"build"})
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
        stage = _stage(enumerate_targets=lambda _repo, _driver: error)
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
                return_value=(None, HookError("changed_files", "git diff failed")),
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
            rc = pipeline_mod.run_stop_checks(
                REPO, "origin/main", options, _unused_upstream_source
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

    def test_low_temporary_space_stops_before_git_facts(self) -> None:
        """A full temporary volume stops preparation with a notice."""
        temp_dir = Path("/small/tmp")
        collect = mock.Mock()

        def disk_usage(path: Path) -> types.SimpleNamespace:
            """Report a nearly full volume for the temporary directory."""
            free = 0 if path == temp_dir else TESTING_FREE_BYTES
            return _usage(free)

        patches = (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(disk_mod.shutil, "disk_usage", disk_usage),
            mock.patch.object(pipeline_mod, "collect_git_facts", collect),
        )
        with _enter_patchers(patches):
            preparation = pipeline_mod.prepare_run_stop_checks(
                REPO,
                "origin/main",
                always_fetch=False,
                config=Config(),
                min_free_mb=100,
                temp_dir=temp_dir,
            )

        match preparation:
            case state_mod.PreparationStop(notice=notice):
                pass
            case _:
                pytest.fail(f"expected a quiet stop but got {preparation!r}")
        assert notice is not None, "expected a skip notice for the operator"
        assert str(temp_dir) in notice, (
            f"expected the filled volume in the notice but got {notice!r}"
        )
        assert "0 B free" in notice, (
            f"expected the measured free space in the notice but got {notice!r}"
        )
        assert "quality gates skipped" in notice, (
            f"expected the notice to say the gates were skipped but got {notice!r}"
        )
        collect.assert_not_called()

    def test_low_repository_space_stops_before_git_facts(self) -> None:
        """A full repository volume stops preparation with a notice."""
        collect = mock.Mock()
        patches = (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                disk_mod.shutil,
                "disk_usage",
                return_value=_usage(1024 * 1024),
            ),
            mock.patch.object(pipeline_mod, "collect_git_facts", collect),
        )
        with _enter_patchers(patches):
            preparation = pipeline_mod.prepare_run_stop_checks(
                REPO,
                "origin/main",
                always_fetch=False,
                config=Config(),
                min_free_mb=100,
                temp_dir=Path("/scratch/tmp"),
            )

        match preparation:
            case state_mod.PreparationStop(notice=notice):
                pass
            case _:
                pytest.fail(f"expected a quiet stop but got {preparation!r}")
        assert notice is not None, "expected a skip notice for the operator"
        assert str(REPO) in notice, (
            f"expected the filled volume in the notice but got {notice!r}"
        )
        collect.assert_not_called()

    def test_ample_space_proceeds_to_git_facts(self) -> None:
        """A threshold met by both volumes leaves preparation unchanged."""
        collect = mock.Mock(return_value=GIT_FACTS)
        patches = (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(pipeline_mod, "collect_git_facts", collect),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(pipeline_mod, "changed_files", return_value=((), None)),
        )
        with _enter_patchers(patches):
            preparation = pipeline_mod.prepare_run_stop_checks(
                REPO,
                "origin/main",
                always_fetch=False,
                config=Config(),
                min_free_mb=100,
                temp_dir=Path("/scratch/tmp"),
            )

        match preparation:
            case state_mod.PreparationReady():
                pass
            case _:
                pytest.fail(f"expected preparation to proceed but got {preparation!r}")
        collect.assert_called_once()

    @pytest.mark.parametrize(
        ("min_free_mb", "temp_dir"),
        [
            (None, None),
            (0, Path("/scratch/tmp")),
        ],
        ids=["no-settings", "zero-threshold"],
    )
    def test_probe_is_skipped_without_a_threshold(
        self, min_free_mb: int | None, temp_dir: Path | None
    ) -> None:
        """Omitting the settings, or setting a zero floor, never probes."""
        probe = mock.Mock()
        patches = (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(disk_mod.shutil, "disk_usage", probe),
            mock.patch.object(
                pipeline_mod, "collect_git_facts", return_value=GIT_FACTS
            ),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(pipeline_mod, "changed_files", return_value=((), None)),
        )
        with _enter_patchers(patches):
            preparation = pipeline_mod.prepare_run_stop_checks(
                REPO,
                "origin/main",
                always_fetch=False,
                config=Config(),
                min_free_mb=min_free_mb,
                temp_dir=temp_dir,
            )

        match preparation:
            case state_mod.PreparationReady():
                pass
            case _:
                pytest.fail(f"expected preparation to proceed but got {preparation!r}")
        probe.assert_not_called()

    def test_probe_failure_is_logged_and_preparation_continues(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unreadable volume cannot be judged full, so the hook continues."""
        error = OSError("No such file or directory")
        probe = mock.Mock(side_effect=error)
        patches = (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(disk_mod.shutil, "disk_usage", probe),
            mock.patch.object(
                pipeline_mod, "collect_git_facts", return_value=GIT_FACTS
            ),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(pipeline_mod, "changed_files", return_value=((), None)),
        )
        with _enter_patchers(patches):
            preparation = pipeline_mod.prepare_run_stop_checks(
                REPO,
                "origin/main",
                always_fetch=False,
                config=Config(),
                min_free_mb=100,
                temp_dir=Path("/scratch/tmp"),
            )

        match preparation:
            case state_mod.PreparationReady():
                pass
            case _:
                pytest.fail(f"expected preparation to proceed but got {preparation!r}")
        records = [r for r in caplog.records if r.name == pipeline_mod.__name__]
        messages = [r.getMessage() for r in records]
        assert messages == [
            f"Could not read free space for {REPO}: {error}",
            f"Could not read free space for {Path('/scratch/tmp')}: {error}",
        ], f"expected one warning per unreadable volume but got {messages!r}"

    def test_run_stop_checks_prints_the_notice_and_allows_the_stop(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The orchestrator writes the skip notice to stderr and exits zero."""
        options = state_mod.StopCheckOptions(
            always_fetch=False,
            max_out=12000,
            min_free_mb=100,
            temp_dir=Path("/small/tmp"),
        )
        with (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(disk_mod.shutil, "disk_usage", return_value=_usage(0)),
        ):
            rc = pipeline_mod.run_stop_checks(
                REPO, "origin/main", options, _unused_upstream_source
            )

        captured = capsys.readouterr()
        assert rc == 0, f"expected return 0 but got {rc!r}"
        assert captured.out == "", "expected no blocking payload on stdout"
        assert "quality gates skipped" in captured.err, (
            f"expected the skip notice on stderr but got {captured.err!r}"
        )

    def test_quiet_stop_writes_no_notice(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A non-repository stop keeps stderr as quiet as stdout."""
        options = state_mod.StopCheckOptions(always_fetch=False, max_out=12000)
        with (
            mock.patch.object(
                pipeline_mod, "_is_executable_available", return_value=True
            ),
            mock.patch.object(
                pipeline_mod,
                "repo_root",
                return_value=(None, HookError("repo_root", "not a git repository")),
            ),
        ):
            rc = pipeline_mod.run_stop_checks(
                Path("/not-a-repo"), "origin/main", options, _unused_upstream_source
            )

        captured = capsys.readouterr()
        assert rc == 0, f"expected return 0 but got {rc!r}"
        assert captured.out == "", "expected no stdout on a quiet stop"
        assert captured.err == "", "expected no notice on a quiet stop"

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
            _unused_upstream_source,
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


class TestRunStopChecksDiskExhaustion:
    """Disk exhaustion during quality checks allows the stop with a notice."""

    def test_exhausted_code_group_allows_the_stop(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A gate that failed only for lack of space does not block."""
        result = _run_stop_checks_with_quality(
            {"check-fmt", "lint", "typecheck"},
            [
                _command(
                    kind="code",
                    exit_code=1,
                    cmd="make --no-print-directory check-fmt lint typecheck",
                    disk_exhausted=True,
                )
            ],
        )

        captured = capsys.readouterr()
        assert result.rc == 0, f"expected return 0 but got {result.rc!r}"
        assert captured.out == "", "expected no blocking payload on stdout"
        assert "code" in captured.err, (
            f"expected the affected group in the notice but got {captured.err!r}"
        )
        assert "disk" in captured.err.casefold(), (
            f"expected the notice to name disk exhaustion but got {captured.err!r}"
        )
        assert str(REPO) in captured.err, (
            f"expected the repository in the notice but got {captured.err!r}"
        )
        result.uncommitted.assert_not_called()
        result.unpushed.assert_not_called()
        result.rebase.assert_not_called()

    def test_exhausted_markdown_group_allows_the_stop(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An indeterminate Markdown group is terminal like a code group."""
        result = _run_stop_checks_with_quality(
            {"markdownlint", "nixie"},
            [
                _command(
                    kind="markdown",
                    exit_code=1,
                    cmd="make --no-print-directory markdownlint nixie",
                    disk_exhausted=True,
                )
            ],
            changed_files=["docs/users-guide.md"],
        )

        captured = capsys.readouterr()
        assert result.rc == 0, f"expected return 0 but got {result.rc!r}"
        assert captured.out == "", "expected no blocking payload on stdout"
        assert "markdown" in captured.err, (
            f"expected the affected group in the notice but got {captured.err!r}"
        )
        result.uncommitted.assert_not_called()

    def test_mixed_exhaustion_and_a_genuine_failure_still_blocks(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """One real failure among exhausted groups keeps the block contract."""
        result = _run_stop_checks_with_quality(
            {
                "check-fmt",
                "lint",
                "typecheck",
                "markdownlint",
                "nixie",
            },
            [
                _command(
                    kind="code",
                    exit_code=1,
                    cmd="make --no-print-directory check-fmt lint typecheck",
                    disk_exhausted=True,
                ),
                _command(
                    kind="markdown",
                    exit_code=2,
                    cmd="make --no-print-directory markdownlint nixie",
                ),
            ],
        )

        captured = capsys.readouterr()
        assert result.rc == 0, f"expected return 0 but got {result.rc!r}"
        payload = json.loads(captured.out)
        assert payload["decision"] == "block", (
            f"expected a blocking decision but got {payload['decision']!r}"
        )
        assert "Command failed (exit 2)" in payload["reason"], (
            f"expected the genuine failure in the reason but got {payload['reason']!r}"
        )
        result.uncommitted.assert_not_called()


# ---------------------------------------------------------------------------
# QualityEvaluation disk-exhaustion classification

# ---------------------------------------------------------------------------


def _evaluation_with(
    *commands: exec_mod.CommandResult, error: HookError | None = None
) -> state_mod.QualityEvaluation:
    """Build an evaluation from prepared command results."""
    return state_mod.QualityEvaluation(
        categories=frozenset({"code"}),
        build_driver="make",
        commands=commands,
        error=error,
    )


def _exhausted(kind: str = "code") -> exec_mod.CommandResult:
    """Build a failed command whose output reports a full disk."""
    return exec_mod.CommandResult(
        kind=kind,
        cmd="make --no-print-directory lint",
        exit_code=1,
        stdout="",
        stderr="No space left on device",
        disk_exhausted=True,
    )


class TestQualityEvaluationDiskExhaustion:
    """Disk exhaustion makes a failing evaluation indeterminate, not blocking."""

    def test_every_failure_showing_disk_exhaustion_is_indeterminate(self) -> None:
        """A run whose failures are all disk exhaustion allows the stop."""
        evaluation = _evaluation_with(_exhausted("code"), _exhausted("markdown"))

        assert evaluation.indeterminate is True, (
            f"expected an indeterminate evaluation but got {evaluation!r}"
        )
        assert evaluation.blocked is False, (
            f"expected the indeterminate evaluation not to block but got {evaluation!r}"
        )

    def test_a_passing_group_does_not_prevent_indeterminacy(self) -> None:
        """A passing group alongside an exhausted failure still allows the stop."""
        evaluation = _evaluation_with(_exhausted(), _command(kind="markdown"))

        assert evaluation.indeterminate is True, (
            f"expected the passing group not to block indeterminacy but got "
            f"{evaluation!r}"
        )

    def test_mixed_failures_still_block(self) -> None:
        """A genuine failure alongside disk exhaustion remains a block."""
        genuine = _command(kind="markdown", exit_code=1)

        evaluation = _evaluation_with(_exhausted(), genuine)

        assert evaluation.indeterminate is False, (
            f"expected a mixed failure not to be indeterminate but got {evaluation!r}"
        )
        assert evaluation.blocked is True, (
            f"expected the mixed failure to block but got {evaluation!r}"
        )

    def test_disk_exhaustion_message_without_the_flag_still_blocks(self) -> None:
        """The classification belongs to the adapter, not to the raw text."""
        unclassified = exec_mod.CommandResult(
            kind="code",
            cmd="make --no-print-directory lint",
            exit_code=1,
            stdout="",
            stderr="No space left on device",
        )

        evaluation = _evaluation_with(unclassified)

        assert evaluation.indeterminate is False, (
            f"expected an unclassified failure to block but got {evaluation!r}"
        )
        assert evaluation.blocked is True, (
            f"expected the unclassified failure to block but got {evaluation!r}"
        )

    def test_enumeration_error_showing_disk_exhaustion_is_indeterminate(self) -> None:
        """A target read that failed for lack of space allows the stop."""
        evaluation = _evaluation_with(
            error=HookError("get_make_targets", "write failed: ENOSPC")
        )

        assert evaluation.indeterminate is True, (
            f"expected the enumeration failure to be indeterminate but got "
            f"{evaluation!r}"
        )
        assert evaluation.blocked is False, (
            f"expected the enumeration failure not to block but got {evaluation!r}"
        )

    def test_other_enumeration_error_still_blocks(self) -> None:
        """An ordinary enumeration failure keeps blocking the stop."""
        evaluation = _evaluation_with(
            error=HookError("get_make_targets", "permission denied")
        )

        assert evaluation.indeterminate is False, (
            f"expected the enumeration failure to block but got {evaluation!r}"
        )
        assert evaluation.blocked is True, (
            f"expected the enumeration failure to block but got {evaluation!r}"
        )

    def test_no_failures_are_never_indeterminate(self) -> None:
        """A passing evaluation is a pass, not an indeterminate outcome."""
        evaluation = _evaluation_with(_command())

        assert evaluation.indeterminate is False, (
            f"expected a passing evaluation not to be indeterminate but got "
            f"{evaluation!r}"
        )
        assert evaluation.blocked is False, (
            f"expected a passing evaluation not to block but got {evaluation!r}"
        )

    def test_an_empty_evaluation_is_never_indeterminate(self) -> None:
        """No commands and no error is a pass with nothing to classify."""
        evaluation = state_mod.QualityEvaluation()

        assert evaluation.indeterminate is False, (
            f"expected an empty evaluation not to be indeterminate but got "
            f"{evaluation!r}"
        )
        assert evaluation.blocked is False, (
            f"expected an empty evaluation not to block but got {evaluation!r}"
        )


# ---------------------------------------------------------------------------
# QualityEvaluation properties

# ---------------------------------------------------------------------------


class TestQualityEvaluationProperties:
    """Hypothesis properties for the immutable quality result."""

    @given(
        st.lists(
            st.tuples(st.integers(min_value=-5, max_value=5), st.booleans()),
            max_size=4,
        ),
        st.booleans(),
        st.booleans(),
    )
    def test_failure_classification_follows_errors_and_exit_codes(
        self,
        commands: list[tuple[int, bool]],
        has_error: bool,
        error_is_exhaustion: bool,
    ) -> None:
        """Blocking needs a failure, and not every failure disk-exhausted."""
        error = (
            HookError(
                "test",
                "No space left on device" if error_is_exhaustion else "boom",
            )
            if has_error
            else None
        )
        results = tuple(
            _command(exit_code=code, disk_exhausted=flag) for code, flag in commands
        )
        evaluation = state_mod.QualityEvaluation(
            categories=frozenset({"code"}),
            commands=results,
            error=error,
        )

        failure_flags = [
            command.disk_exhausted for command in results if command.exit_code != 0
        ]
        if error is not None:
            failure_flags.append(error_is_exhaustion)
        has_failures = bool(failure_flags)
        indeterminate = has_failures and all(failure_flags)

        assert evaluation.indeterminate is indeterminate
        assert evaluation.blocked is (has_failures and not indeterminate)

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
