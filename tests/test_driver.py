"""Exercise build-driver discovery and make-target parsing."""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404
from pathlib import Path
from unittest import mock

import pytest

from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import execution as exec_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import state as state_mod

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

        assert targets == {"check-fmt", "lint", "typecheck", "docs", "_internal-target"}

    def test_get_make_targets_reads_makefile_without_running_make(
        self, tmp_path: Path
    ) -> None:
        """Make target enumeration reads Makefile text directly."""
        (tmp_path / "Makefile").write_text("check-fmt:\nlint:\n", encoding="utf-8")
        with mock.patch.object(driver_mod, "run") as mock_run:
            targets, err = driver_mod.get_make_targets(tmp_path)
        assert err is None
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
            targets, err = driver_mod.get_make_targets(tmp_path)

        assert err is None
        assert targets is not None
        assert "check-fmt" in targets
        assert "lint" in targets
        mock_run.assert_not_called()
        assert not side_effect.exists()


# ---------------------------------------------------------------------------


class TestGetNetsukeTargets:
    """Tests for get_netsuke_targets()."""

    def test_returns_targets(self) -> None:
        """Successful netsuke manifest call -> parsed target set."""
        with mock.patch.object(driver_mod, "run") as mock_run:
            mock_run.return_value = _completed(
                0,
                stdout="\n".join([
                    "ninja_required_version = 1.11",
                    "build check-fmt: phony",
                    "build lint: phony",
                ]),
            )
            targets, err = driver_mod.get_netsuke_targets(REPO)
        assert targets == {"check-fmt", "lint"}, (
            f"expected target set but got {targets!r}"
        )
        assert err is None, f"expected no error but got {err!r}"
        mock_run.assert_called_once_with(["netsuke", "manifest", "-"], REPO)

    def test_returns_error_on_failure(self) -> None:
        """Non-zero exit from netsuke -> error message."""
        with mock.patch.object(driver_mod, "run") as mock_run:
            mock_run.return_value = _completed(1, stderr="netsuke: not found")
            targets, err = driver_mod.get_netsuke_targets(REPO)
        assert targets is None, f"expected no targets on failure but got {targets!r}"
        assert "netsuke: not found" in (err or ""), (
            f"expected error message but got {err!r}"
        )
        mock_run.assert_called_once_with(["netsuke", "manifest", "-"], REPO)

    def test_returns_error_when_executable_missing(self) -> None:
        """FileNotFoundError -> error message, no targets."""
        with mock.patch.object(
            driver_mod,
            "run",
            side_effect=FileNotFoundError(2, "No such file or directory", "netsuke"),
        ):
            targets, err = driver_mod.get_netsuke_targets(REPO)
        assert targets is None, (
            f"expected no targets when netsuke is missing but got {targets!r}"
        )
        assert "netsuke not found on PATH" in (err or ""), (
            f"expected missing-executable error but got {err!r}"
        )


# ---------------------------------------------------------------------------
# TestBuildDriverSelection


class TestBuildDriverSelection:
    """Tests for build-driver selection."""

    def test_auto_prefers_netsuke_when_both_manifests_exist(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "Netsukefile").write_text("actions: {}\n", encoding="utf-8")
        (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
        with mock.patch.object(shutil, "which", return_value="/usr/bin/tool"):
            driver, err = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )
        assert err is None
        assert driver is not None
        assert driver.name == "netsuke"

    def test_auto_uses_make_when_only_makefile_exists(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
        with mock.patch.object(shutil, "which", return_value="/usr/bin/make"):
            driver, err = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )
        assert err is None
        assert driver is not None
        assert driver.name == "make"

    def test_make_override_uses_make_when_netsuke_exists(self, tmp_path: Path) -> None:
        (tmp_path / "Netsukefile").write_text("actions: {}\n", encoding="utf-8")
        (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
        with mock.patch.object(shutil, "which", return_value="/usr/bin/make"):
            driver, err = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                    build_driver="make",
                ),
            )
        assert err is None
        assert driver is not None
        assert driver.name == "make"

    def test_netsuke_override_errors_when_binary_missing(self, tmp_path: Path) -> None:
        (tmp_path / "Netsukefile").write_text("actions: {}\n", encoding="utf-8")
        with mock.patch.object(shutil, "which", return_value=None):
            driver, err = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                    build_driver="netsuke",
                ),
            )
        assert driver is None
        assert err is not None
        assert "netsuke not found" in err

    def test_auto_skips_when_no_supported_manifest_exists(self, tmp_path: Path) -> None:
        """No Makefile or Netsukefile in auto mode means no quality driver."""
        with mock.patch.object(shutil, "which", return_value="/usr/bin/tool"):
            driver, err = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )

        assert driver is None
        assert err is None


class TestNetsukeTargets:
    """Tests for Netsuke target enumeration and execution."""

    def test_parse_netsuke_targets_from_manifest(self) -> None:
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
                return_value=({"build"}, None),
            ),
            mock.patch.object(pipeline_mod, "run_build_targets") as mock_run_targets,
        ):
            rc = pipeline_mod.evaluate_changes(state, REPO, 12000, driver)

        assert rc == 0
        assert state.ok is True
        assert state.targets_requested == ["check-fmt", "lint", "typecheck"]
        assert state.targets_run == []
        assert state.targets_skipped == ["check-fmt", "lint", "typecheck"]
        mock_run_targets.assert_not_called()

    def test_markdown_changes_run_netsuke_markdownlint(self) -> None:
        state = state_mod.HookState(changed_files=["docs/users-guide.md"])
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with (
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value=({"check-fmt", "lint", "markdownlint"}, None),
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
            rc = pipeline_mod.evaluate_changes(state, REPO, 12000, driver)
        assert rc == 0
        mock_run_targets.assert_called_once_with(
            REPO,
            exec_mod.BuildTargetRequest(driver, "markdown", ["markdownlint"]),
            12000,
        )

    def test_rust_changes_run_netsuke_code_targets(self) -> None:
        state = state_mod.HookState(changed_files=["src/lib.rs"])
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with (
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value=({"check-fmt", "lint", "markdownlint"}, None),
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
            rc = pipeline_mod.evaluate_changes(state, REPO, 12000, driver)
        assert rc == 0
        mock_run_targets.assert_called_once_with(
            REPO,
            exec_mod.BuildTargetRequest(driver, "code", ["check-fmt", "lint"]),
            12000,
        )

    def test_python_changes_run_netsuke_typecheck(self) -> None:
        state = state_mod.HookState(changed_files=["src/app.py"])
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with (
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value=({"check-fmt", "lint", "typecheck"}, None),
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
            rc = pipeline_mod.evaluate_changes(state, REPO, 12000, driver)
        assert rc == 0
        mock_run_targets.assert_called_once_with(
            REPO,
            exec_mod.BuildTargetRequest(
                driver, "code", ["check-fmt", "lint", "typecheck"]
            ),
            12000,
        )

    def test_failure_output_uses_build_target_label(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = state_mod.HookState(changed_files=["docs/users-guide.md"])
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with (
            mock.patch.object(
                pipeline_mod,
                "get_build_targets",
                return_value=({"markdownlint"}, None),
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
            rc = pipeline_mod.evaluate_changes(state, REPO, 12000, driver)
        assert rc == exec_mod.BLOCKED_STATUS
        out = json.loads(capsys.readouterr().out)
        assert "Requested build targets: markdownlint" in out["reason"]
        assert "Command failed (exit 1): netsuke build markdownlint" in out["reason"]


# ---------------------------------------------------------------------------
# truncate
