"""Exercise build execution, target selection, and output capture."""

from __future__ import annotations

import subprocess  # noqa: S404
from pathlib import Path
from unittest import mock

from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import execution as exec_mod
from post_turn_quality_stop_hook import formatting as formatting_mod
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
# has_uncommitted_changes

# ---------------------------------------------------------------------------


class TestDedupPreserveOrder:
    """Tests for dedup_preserve_order()."""

    def test_empty_list(self) -> None:
        result = exec_mod.dedup_preserve_order([])
        assert result == [], f"expected empty list but got {result!r}"

    def test_no_duplicates(self) -> None:
        result = exec_mod.dedup_preserve_order(["a", "b", "c"])
        assert result == ["a", "b", "c"], f"expected unchanged list but got {result!r}"

    def test_with_duplicates(self) -> None:
        result = exec_mod.dedup_preserve_order(["a", "b", "a", "c", "b"])
        assert result == ["a", "b", "c"], (
            f"expected duplicates removed but got {result!r}"
        )


# ---------------------------------------------------------------------------
# build_command

# ---------------------------------------------------------------------------


class TestBuildCommand:
    """Tests for build_command()."""

    def test_make_driver(self) -> None:
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        cmd = exec_mod.build_command(driver, ["fmt", "lint"])
        assert cmd == ["make", "--no-print-directory", "fmt", "lint"], (
            f"expected make command but got {cmd!r}"
        )

    def test_netsuke_driver(self) -> None:
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        cmd = exec_mod.build_command(driver, ["build"])
        assert cmd == ["netsuke", "build", "build"], (
            f"expected netsuke build command but got {cmd!r}"
        )


# ---------------------------------------------------------------------------
# parse_bool_env

# ---------------------------------------------------------------------------


class TestRunBuildTargets:
    """Tests for run_build_targets()."""

    def test_returns_command_result(self) -> None:
        """Successful run -> a CommandResult dict."""
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        request = exec_mod.BuildTargetRequest(driver, "code", ["check-fmt", "lint"])
        with mock.patch.object(exec_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="ok\n", stderr="")
            result = exec_mod.run_build_targets(REPO, request, 12000)
        assert result["kind"] == "code", (
            f"expected kind code but got {result['kind']!r}"
        )
        assert result["exit_code"] == 0, (
            f"expected exit_code 0 but got {result['exit_code']}"
        )
        assert "ok" in result["stdout"], (
            f"expected stdout to contain ok but got {result['stdout']!r}"
        )

    def test_passes_correct_make_command(self) -> None:
        """Make driver -> [executable, --no-print-directory, targets...]."""
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        request = exec_mod.BuildTargetRequest(driver, "code", ["check-fmt", "lint"])
        with mock.patch.object(exec_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="", stderr="")
            exec_mod.run_build_targets(REPO, request, 12000)
        mock_run.assert_called_once_with(
            ["make", "--no-print-directory", "check-fmt", "lint"], REPO
        )

    def test_passes_correct_netsuke_command(self) -> None:
        """Netsuke driver -> [executable, build, targets...]."""
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        request = exec_mod.BuildTargetRequest(driver, "code", ["check-fmt", "lint"])
        with mock.patch.object(exec_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="", stderr="")
            exec_mod.run_build_targets(REPO, request, 12000)
        mock_run.assert_called_once_with(
            ["netsuke", "build", "check-fmt", "lint"], REPO
        )

    def test_captures_stdout_and_stderr(self) -> None:
        """Non-empty stdout and stderr -> both present in result."""
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        request = exec_mod.BuildTargetRequest(driver, "code", ["check-fmt"])
        with mock.patch.object(exec_mod, "run") as mock_run:
            mock_run.return_value = _completed(
                0, stdout="all good\n", stderr="warning: something\n"
            )
            result = exec_mod.run_build_targets(REPO, request, 12000)
        assert result["stdout"] == "all good\n", (
            f"expected stdout but got {result['stdout']!r}"
        )
        assert "warning: something" in result["stderr"], (
            f"expected stderr but got {result['stderr']!r}"
        )

    def test_empty_targets_skips_run(self) -> None:
        """Empty target list -> skips subprocess, returns sentinel result."""
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        request = exec_mod.BuildTargetRequest(driver, "code", [])
        with mock.patch.object(exec_mod, "run") as mock_run:
            result = exec_mod.run_build_targets(REPO, request, 12000)
        mock_run.assert_not_called()
        assert result["exit_code"] == 0, (
            f"expected exit_code 0 but got {result['exit_code']}"
        )
        assert result["cmd"] == "", f"expected empty cmd but got {result['cmd']!r}"

    def test_handles_file_not_found(self) -> None:
        """FileNotFoundError from run -> exit_code 127, error in stderr."""
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        request = exec_mod.BuildTargetRequest(driver, "code", ["check-fmt"])
        with mock.patch.object(
            exec_mod,
            "run",
            side_effect=FileNotFoundError(2, "No such file or directory", "make"),
        ):
            result = exec_mod.run_build_targets(REPO, request, 12000)
        enoent_exit = 127
        assert result["exit_code"] == enoent_exit, (
            f"expected exit_code {enoent_exit} but got {result['exit_code']}"
        )
        assert "make not found" in result["stderr"], (
            f"expected not-found error but got {result['stderr']!r}"
        )


# ---------------------------------------------------------------------------


class TestTargetsForCategories:
    """Tests for targets_for_categories()."""

    def test_all_false_returns_empty(self) -> None:
        cats = {"python_ts": False, "rust": False, "markdown": False}
        result = exec_mod.targets_for_categories(cats)
        assert result == [], f"expected empty list but got {result!r}"

    def test_python_true_returns_targets(self) -> None:
        cats = {"python_ts": True, "rust": False, "markdown": False}
        result = exec_mod.targets_for_categories(cats)
        assert len(result) > 0, f"expected non-empty list but got {result!r}"

    def test_markdown_true_returns_targets(self) -> None:
        cats = {"python_ts": False, "rust": False, "markdown": True}
        result = exec_mod.targets_for_categories(cats)
        assert len(result) > 0, f"expected non-empty list but got {result!r}"

    def test_include_filter(self) -> None:
        cats = {"python_ts": True, "rust": False, "markdown": True}
        result = exec_mod.targets_for_categories(cats, include={"python_ts"})
        assert result == ["check-fmt", "lint", "typecheck"], (
            f"expected only python targets but got {result!r}"
        )


# ---------------------------------------------------------------------------
# main() end-to-end

# ---------------------------------------------------------------------------


class TestDefaultCategories:
    """Tests for default_categories()."""

    def test_all_false(self) -> None:
        cats = state_mod.default_categories()
        assert isinstance(cats, dict), f"expected dict but got {type(cats)}"
        for key, val in cats.items():
            assert isinstance(key, str), f"expected str key but got {type(key)}"
            assert val is False, f"expected {key!r} to be False but was {val!r}"


# ---------------------------------------------------------------------------
# detect_categories

# ---------------------------------------------------------------------------


class TestDetectCategories:
    """Tests for detect_categories()."""

    def test_empty_file_list(self) -> None:
        cats = formatting_mod.detect_categories([])
        assert all(v is False for v in cats.values()), (
            f"expected all False for empty list but got {cats}"
        )

    def test_py_file(self) -> None:
        cats = formatting_mod.detect_categories(["src/app.py"])
        assert cats["python_ts"] is True, (
            f"expected python_ts True for .py file but was {cats['python_ts']!r}"
        )
        assert cats["rust"] is False
        assert cats["markdown"] is False

    def test_ts_file(self) -> None:
        cats = formatting_mod.detect_categories(["src/app.ts"])
        assert cats["python_ts"] is True, (
            f"expected python_ts True for .ts file but was {cats['python_ts']!r}"
        )

    def test_md_file(self) -> None:
        cats = formatting_mod.detect_categories(["docs/readme.md"])
        assert cats["markdown"] is True, (
            f"expected markdown True for .md file but was {cats['markdown']!r}"
        )
        assert cats["python_ts"] is False

    def test_mixed_py_and_md(self) -> None:
        cats = formatting_mod.detect_categories(["src/app.py", "docs/readme.md"])
        assert cats["python_ts"] is True
        assert cats["markdown"] is True
        assert cats["rust"] is False


# ---------------------------------------------------------------------------
# dedup_preserve_order

# ---------------------------------------------------------------------------


class TestTruncate:
    """Tests for truncate()."""

    def test_input_shorter_than_max(self) -> None:
        result = exec_mod.truncate("hello", 10)
        assert result == "hello", f"expected unchanged 'hello' but got {result!r}"

    def test_input_exactly_max(self) -> None:
        result = exec_mod.truncate("hello", 5)
        assert result == "hello", f"expected unchanged 'hello' but got {result!r}"

    def test_input_longer_than_max(self) -> None:
        max_chars = 40
        result = exec_mod.truncate("hello world " * 10, max_chars)
        assert len(result) == max_chars, (
            f"expected 40 characters but got {len(result)} ({result!r})"
        )
        assert "... (output truncated) ..." in result, (
            f"expected truncation marker in result but got {result!r}"
        )

    def test_max_zero_returns_empty(self) -> None:
        result = exec_mod.truncate("hello", 0)
        assert result == "", f"expected empty string for max_chars=0 but got {result!r}"


# ---------------------------------------------------------------------------
# default_categories
