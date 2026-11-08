"""Exercise build-driver discovery and make-target parsing."""

from __future__ import annotations

import shutil
import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path
from unittest import mock

import pytest

from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import execution as exec_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook.errors import HookError

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


# ---------------------------------------------------------------------------
# TestGetMakeTargets

# ---------------------------------------------------------------------------


class TestGetMakeTargets:
    """Tests for make target enumeration."""

    def test_parse_makefile_returns_named_targets(self, tmp_path: Path) -> None:
        """Direct Makefile parsing returns named targets with hyphens."""
        makefile = tmp_path / "Makefile"
        makefile.write_text(
            "\n".join([
                ".PHONY: check-fmt lint",
                "check-fmt:",
                "\truff format --check",
                "lint:",
                "\truff check",
                "typecheck: build",
                "docs:",
                "target/file:",
                "_internal-target:",
            ]),
            encoding="utf-8",
        )

        targets = driver_mod.parse_makefile(makefile)

        assert targets == {
            ".PHONY",
            "check-fmt",
            "lint",
            "typecheck",
            "docs",
            "target/file",
            "_internal-target",
        }

    def test_parse_makefile_collects_all_targets_and_make_target_characters(
        self, tmp_path: Path
    ) -> None:
        """Every name in a multi-target rule survives direct Makefile parsing."""
        makefile = tmp_path / "Makefile"
        makefile.write_text(
            "\n".join([
                "lint check-fmt: build",
                "target.with.dots/file+plus@local%pattern?glob*:",
                r"target\ with\ spaces target\:with\:colons:",
                "continued-first \\",
                "    continued-second:",
            ]),
            encoding="utf-8",
        )

        targets = driver_mod.parse_makefile(makefile)

        assert targets == {
            "lint",
            "check-fmt",
            "target.with.dots/file+plus@local%pattern?glob*",
            "target with spaces",
            "target:with:colons",
            "continued-first",
            "continued-second",
        }

    def test_get_make_targets_reads_makefile_without_running_make(
        self, tmp_path: Path
    ) -> None:
        """Make target enumeration reads Makefile text directly."""
        (tmp_path / "Makefile").write_text("check-fmt:\nlint:\n", encoding="utf-8")
        with mock.patch.object(driver_mod, "run") as mock_run:
            targets = driver_mod.get_make_targets(tmp_path)
        assert targets == {"check-fmt", "lint"}
        mock_run.assert_not_called()

    def test_make_target_enumeration_does_not_run_default_goal(
        self, tmp_path: Path
    ) -> None:
        """Enumerating targets must not execute recursive default recipes."""
        if shutil.which("make") is None:
            pytest.skip("make not available")

        side_effect = tmp_path / "side-effect"
        (tmp_path / "Makefile").write_text(
            "\n".join([
                ".PHONY: all check-fmt lint",
                "all:",
                f"\t+touch {side_effect}",
                "check-fmt:",
                "lint:",
                "",
            ]),
            encoding="utf-8",
        )

        with mock.patch.object(driver_mod, "run") as mock_run:
            targets = driver_mod.get_make_targets(tmp_path)

        assert isinstance(targets, set), f"expected a target set but got {targets!r}"
        assert "check-fmt" in targets, (
            f"expected check-fmt among the targets but got {targets!r}"
        )
        assert "lint" in targets, f"expected lint among the targets but got {targets!r}"
        mock_run.assert_not_called()
        assert not side_effect.exists(), (
            f"expected no default-goal side effect at {side_effect}"
        )

    def test_missing_makefile_yields_empty_targets(self, tmp_path: Path) -> None:
        """A repository without a Makefile enumerates no targets and no error."""
        targets = driver_mod.get_make_targets(tmp_path)

        assert targets == set()

    def test_makefile_directory_yields_empty_targets(self, tmp_path: Path) -> None:
        """A directory named ``Makefile`` is not a Makefile, so no targets."""
        (tmp_path / "Makefile").mkdir()

        targets = driver_mod.get_make_targets(tmp_path)

        assert targets == set()

    @pytest.mark.parametrize(
        "exc",
        [
            PermissionError(13, "Permission denied"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ],
        ids=["unreadable", "undecodable"],
    )
    def test_unreadable_makefile_reports_read_error(
        self, tmp_path: Path, exc: Exception
    ) -> None:
        """Read failures surface as a descriptive error rather than propagating."""
        makefile = tmp_path / "Makefile"
        makefile.write_text("lint:\n", encoding="utf-8")

        with mock.patch.object(driver_mod, "parse_makefile", side_effect=exc):
            targets = driver_mod.get_make_targets(tmp_path)

        assert isinstance(targets, HookError), (
            f"expected a HookError but got {targets!r}"
        )
        assert targets.operation == "get_make_targets", (
            f"expected the get_make_targets operation but got {targets.operation!r}"
        )
        assert str(targets).startswith(f"could not read {makefile}: "), (
            f"expected the read failure to name {makefile} but got {str(targets)!r}"
        )


# ---------------------------------------------------------------------------


class TestGetNetsukeTargets:
    """Tests for get_netsuke_targets()."""

    def test_returns_targets(self) -> None:
        """Successful netsuke generate call -> parsed target set."""
        with mock.patch.object(driver_mod, "run") as mock_run:
            mock_run.return_value = _completed(
                0,
                stdout="\n".join([
                    "ninja_required_version = 1.11",
                    "build check-fmt: phony",
                    "build lint: phony",
                ]),
            )
            targets = driver_mod.get_netsuke_targets(REPO)
        assert targets == {"check-fmt", "lint"}, (
            f"expected target set but got {targets!r}"
        )
        mock_run.assert_called_once_with(["netsuke", "generate"], REPO)

    def test_returns_error_on_failure(self) -> None:
        """Non-zero exit from netsuke -> error message."""
        with mock.patch.object(driver_mod, "run") as mock_run:
            mock_run.return_value = _completed(1, stderr="netsuke: not found")
            targets = driver_mod.get_netsuke_targets(REPO)
        assert isinstance(targets, HookError), (
            f"expected an enumeration error but got {targets!r}"
        )
        assert "netsuke: not found" in str(targets), (
            f"expected error message but got {targets!r}"
        )
        mock_run.assert_called_once_with(["netsuke", "generate"], REPO)

    def test_returns_error_when_executable_missing(self) -> None:
        """FileNotFoundError -> error message, no targets."""
        with mock.patch.object(
            driver_mod,
            "run",
            side_effect=FileNotFoundError(2, "No such file or directory", "netsuke"),
        ):
            targets = driver_mod.get_netsuke_targets(REPO)
        assert isinstance(targets, HookError), (
            f"expected an enumeration error when netsuke is missing but got {targets!r}"
        )
        assert "netsuke not found on PATH" in str(targets), (
            f"expected missing-executable error but got {targets!r}"
        )


# ---------------------------------------------------------------------------
# TestNetsukeCliContract

# ---------------------------------------------------------------------------


class TestNetsukeCliContract:
    """Tests against the supported released Netsuke CLI surface."""

    def test_released_generate_outputs_a_ninja_manifest(self, tmp_path: Path) -> None:
        """Released Netsuke exposes generate and emits target build edges."""
        netsuke = shutil.which("netsuke")
        assert netsuke is not None, "netsuke v0.1.0-beta1 is required for this test"

        version = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: executable comes from PATH lookup and arguments are static.
            [netsuke, "--version"],
            capture_output=True,
            check=False,
            text=True,
        )
        assert version.returncode == 0, version.stderr
        assert version.stdout.strip() == "netsuke 0.1.0-beta1"

        (tmp_path / "Netsukefile").write_text(
            "\n".join([
                'netsuke_version: "1.0.0"',
                "",
                "targets: []",
                "",
                "actions:",
                "  - name: check-fmt",
                '    command: "true"',
                "  - name: markdownlint",
                '    command: "true"',
                "",
            ]),
            encoding="utf-8",
        )

        result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: executable comes from PATH lookup and arguments are static.
            [netsuke, "generate"],
            cwd=tmp_path,
            capture_output=True,
            check=False,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert driver_mod.parse_netsuke_targets(result.stdout) == {
            "check-fmt",
            "markdownlint",
        }


# ---------------------------------------------------------------------------
# TestBuildDriverSelection


class TestBuildDriverSelection:
    """Tests for build-driver selection."""

    def test_auto_prefers_netsuke_when_both_manifests_exist(
        self, tmp_path: Path
    ) -> None:
        """Check auto mode prefers netsuke when a Netsukefile and Makefile exist."""
        (tmp_path / "Netsukefile").write_text("actions: {}\n", encoding="utf-8")
        (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
        with mock.patch.object(shutil, "which", return_value="/usr/bin/tool"):
            driver = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )
        assert isinstance(driver, driver_mod.BuildDriver), (
            f"expected a build driver but got {driver!r}"
        )
        assert driver.name == "netsuke", (
            f"expected auto mode to prefer netsuke but got {driver.name!r}"
        )

    def test_auto_uses_make_when_only_makefile_exists(self, tmp_path: Path) -> None:
        """Check auto mode selects make when only a Makefile exists."""
        (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
        with mock.patch.object(shutil, "which", return_value="/usr/bin/make"):
            driver = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )
        assert isinstance(driver, driver_mod.BuildDriver), (
            f"expected a build driver but got {driver!r}"
        )
        assert driver.name == "make", (
            f"expected auto mode to select make but got {driver.name!r}"
        )

    def test_make_override_uses_make_when_netsuke_exists(self, tmp_path: Path) -> None:
        """Check a make override wins over a Netsukefile when both exist."""
        (tmp_path / "Netsukefile").write_text("actions: {}\n", encoding="utf-8")
        (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
        with mock.patch.object(shutil, "which", return_value="/usr/bin/make"):
            driver = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                    build_driver="make",
                ),
            )
        assert isinstance(driver, driver_mod.BuildDriver), (
            f"expected a build driver but got {driver!r}"
        )
        assert driver.name == "make", (
            f"expected the make override to win but got {driver.name!r}"
        )

    def test_netsuke_override_errors_when_binary_missing(self, tmp_path: Path) -> None:
        """Check a netsuke override reports an error when the binary is missing."""
        (tmp_path / "Netsukefile").write_text("actions: {}\n", encoding="utf-8")
        with mock.patch.object(shutil, "which", return_value=None):
            driver = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                    build_driver="netsuke",
                ),
            )
        assert isinstance(driver, HookError), f"expected a HookError but got {driver!r}"
        assert "netsuke not found" in str(driver), (
            f"expected the missing-binary message but got {str(driver)!r}"
        )

    def test_auto_skips_when_no_supported_manifest_exists(self, tmp_path: Path) -> None:
        """No Makefile or Netsukefile in auto mode means no quality driver."""
        with mock.patch.object(shutil, "which", return_value="/usr/bin/tool"):
            driver = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert driver is None, (
            f"expected no driver without a manifest but got {driver!r}"
        )


class TestNetsukeTargets:
    """Tests for Netsuke target enumeration and execution."""

    def test_parse_netsuke_targets_from_manifest(self) -> None:
        """Check build targets are parsed from the build lines of a manifest."""
        targets = driver_mod.parse_netsuke_targets(
            "\n".join([
                "ninja_required_version = 1.11",
                "build check-fmt: phony",
                "build lint: phony",
                "build markdownlint: phony",
            ])
        )
        assert targets == {"check-fmt", "lint", "markdownlint"}

    def test_missing_requested_targets_skip_without_blocking(self) -> None:
        """Absent requested targets are recorded but do not block the hook."""
        state = state_mod.HookState(changed_files=["src/app.py"])
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with (
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value={"build"},
            ),
            mock.patch.object(pipeline_mod, "run_build_targets") as mock_run_targets,
        ):
            evaluation = pipeline_mod.evaluate_changes(state, REPO, 12000, driver)

        assert evaluation.blocked is False, (
            f"expected a non-blocking evaluation but got {evaluation!r}"
        )
        assert evaluation.targets_requested == ("check-fmt", "lint", "typecheck"), (
            f"expected the requested targets to be retained but got "
            f"{evaluation.targets_requested!r}"
        )
        assert evaluation.targets_present == (), (
            f"expected no present targets but got {evaluation.targets_present!r}"
        )
        assert evaluation.targets_skipped == ("check-fmt", "lint", "typecheck"), (
            f"expected every requested target to be skipped but got "
            f"{evaluation.targets_skipped!r}"
        )
        assert evaluation.commands == (), (
            f"expected no commands to run but got {evaluation.commands!r}"
        )
        mock_run_targets.assert_not_called()

    def test_markdown_changes_run_netsuke_markdownlint(self) -> None:
        """Check markdown changes run the markdownlint target through netsuke."""
        state = state_mod.HookState(changed_files=["docs/users-guide.md"])
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with (
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value={"check-fmt", "lint", "markdownlint"},
            ),
            mock.patch.object(
                pipeline_mod,
                "run_build_targets",
                return_value={
                    "kind": "markdown",
                    "cmd": "netsuke build markdownlint",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                },
            ) as mock_run_targets,
        ):
            evaluation = pipeline_mod.evaluate_changes(state, REPO, 12000, driver)
        assert evaluation.blocked is False, (
            f"expected a non-blocking evaluation but got {evaluation!r}"
        )
        assert evaluation.targets_present == ("markdownlint",), (
            f"expected markdownlint to be selected but got "
            f"{evaluation.targets_present!r}"
        )
        mock_run_targets.assert_called_once_with(
            REPO,
            exec_mod.BuildTargetRequest(driver, "markdown", ["markdownlint"]),
            12000,
        )

    def test_rust_changes_run_netsuke_code_targets(self) -> None:
        """Check Rust changes run the code targets through netsuke."""
        state = state_mod.HookState(changed_files=["src/lib.rs"])
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with (
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value={"check-fmt", "lint", "markdownlint"},
            ),
            mock.patch.object(
                pipeline_mod,
                "run_build_targets",
                return_value={
                    "kind": "code",
                    "cmd": "netsuke build check-fmt lint",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                },
            ) as mock_run_targets,
        ):
            evaluation = pipeline_mod.evaluate_changes(state, REPO, 12000, driver)
        assert evaluation.blocked is False, (
            f"expected a non-blocking evaluation but got {evaluation!r}"
        )
        assert evaluation.targets_present == ("check-fmt", "lint"), (
            f"expected the code targets to be selected but got "
            f"{evaluation.targets_present!r}"
        )
        mock_run_targets.assert_called_once_with(
            REPO,
            exec_mod.BuildTargetRequest(driver, "code", ["check-fmt", "lint"]),
            12000,
        )

    def test_python_changes_run_netsuke_typecheck(self) -> None:
        """Check Python changes run the code targets, including typecheck."""
        state = state_mod.HookState(changed_files=["src/app.py"])
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with (
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value={"check-fmt", "lint", "typecheck"},
            ),
            mock.patch.object(
                pipeline_mod,
                "run_build_targets",
                return_value={
                    "kind": "code",
                    "cmd": "netsuke build check-fmt lint typecheck",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                },
            ) as mock_run_targets,
        ):
            evaluation = pipeline_mod.evaluate_changes(state, REPO, 12000, driver)
        assert evaluation.blocked is False, (
            f"expected a non-blocking evaluation but got {evaluation!r}"
        )
        assert evaluation.targets_present == ("check-fmt", "lint", "typecheck"), (
            f"expected typecheck to be selected for Python changes but got "
            f"{evaluation.targets_present!r}"
        )
        mock_run_targets.assert_called_once_with(
            REPO,
            exec_mod.BuildTargetRequest(
                driver, "code", ["check-fmt", "lint", "typecheck"]
            ),
            12000,
        )

    def test_failed_build_target_is_recorded_in_the_evaluation(self) -> None:
        """A failed build target is recorded with its label and command."""
        state = state_mod.HookState(changed_files=["docs/users-guide.md"])
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with (
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value={"markdownlint"},
            ),
            mock.patch.object(
                pipeline_mod,
                "run_build_targets",
                return_value={
                    "kind": "markdown",
                    "cmd": "netsuke build markdownlint",
                    "exit_code": 1,
                    "stdout": "bad docs",
                    "stderr": "",
                },
            ),
        ):
            evaluation = pipeline_mod.evaluate_changes(state, REPO, 12000, driver)
        assert evaluation.blocked is True, (
            f"expected a failed target to block but got {evaluation!r}"
        )
        assert evaluation.commands == (
            {
                "kind": "markdown",
                "cmd": "netsuke build markdownlint",
                "exit_code": 1,
                "stdout": "bad docs",
                "stderr": "",
            },
        )


# ---------------------------------------------------------------------------
# truncate
