"""Exercise CLI entry point: env parsing, main(), cwd resolution."""

from __future__ import annotations

import io
import typing as typ
from unittest import mock

if typ.TYPE_CHECKING:
    from pathlib import Path

import pytest

from post_turn_quality_stop_hook import hook
from post_turn_quality_stop_hook import pipeline as pipeline_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# parse_bool_env

# ---------------------------------------------------------------------------


class TestParseBoolEnv:
    """Tests for parse_bool_env()."""

    def test_empty_string(self) -> None:
        assert hook.parse_bool_env("") is False

    def test_zero(self) -> None:
        assert hook.parse_bool_env("0") is False

    def test_one(self) -> None:
        assert hook.parse_bool_env("1") is True

    def test_true(self) -> None:
        assert hook.parse_bool_env("true") is True

    def test_yes(self) -> None:
        assert hook.parse_bool_env("yes") is True

    def test_false(self) -> None:
        assert hook.parse_bool_env("false") is False


# ---------------------------------------------------------------------------
# parse_max_output

# ---------------------------------------------------------------------------


class TestParseMaxOutput:
    """Tests for parse_max_output()."""

    def test_valid_integer(self) -> None:
        result = hook.parse_max_output("5000")
        expected = 5000
        assert result == expected, f"expected {expected} but got {result!r}"

    def test_zero(self) -> None:
        result = hook.parse_max_output("0")
        assert result == 0, f"expected 0 but got {result!r}"

    def test_negative_input_returns_default(self) -> None:
        default = 123
        result = hook.parse_max_output("-100", default=default)
        assert result == default, (
            f"expected default {default} for negative input but got {result!r}"
        )

    def test_non_integer_returns_default(self) -> None:
        result = hook.parse_max_output("abc", default=999)
        expected = 999
        assert result == expected, f"expected default {expected} but got {result!r}"

    def test_empty_string_returns_default(self) -> None:
        result = hook.parse_max_output("", default=42)
        expected = 42
        assert result == expected, f"expected default {expected} but got {result!r}"


# ---------------------------------------------------------------------------
# parse_hook_input

# ---------------------------------------------------------------------------


class TestParseHookInput:
    """Tests for parse_hook_input()."""

    def test_empty_stdin(self) -> None:
        with mock.patch("sys.stdin", io.StringIO("")):
            result = hook.parse_hook_input()
        assert result == {}, f"expected empty dict but got {result!r}"

    def test_valid_json(self) -> None:
        with mock.patch("sys.stdin", io.StringIO('{"cwd": "/some/project"}')):
            result = hook.parse_hook_input()
        expected = {"cwd": "/some/project"}
        assert result == expected, f"expected {expected} but got {result!r}"

    def test_invalid_json_returns_empty(self) -> None:
        with mock.patch("sys.stdin", io.StringIO("not json")):
            result = hook.parse_hook_input()
        assert result == {}, f"expected empty dict for invalid json but got {result!r}"


# ---------------------------------------------------------------------------
# resolve_start_cwd

# ---------------------------------------------------------------------------


class TestResolveStartCwd:
    """Tests for resolve_start_cwd()."""

    def test_cwd_from_hook_input(self, tmp_path: Path) -> None:
        result = hook.resolve_start_cwd(hook.HookInput(cwd=str(tmp_path / "project")))
        assert result == tmp_path / "project", (
            f"expected {tmp_path / 'project'} but got {result!r}"
        )

    def test_claude_project_dir_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "claude-project"))
        result = hook.resolve_start_cwd(hook.HookInput())
        assert result == tmp_path / "claude-project", (
            f"expected {tmp_path / 'claude-project'} but got {result!r}"
        )

    def test_falls_back_to_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        result = hook.resolve_start_cwd(hook.HookInput())
        assert result == tmp_path, f"expected {tmp_path} but got {result!r}"

    def test_empty_cwd_falls_back_to_project_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "env-project"))
        result = hook.resolve_start_cwd(
            hook.HookInput(cwd="", project_dir=str(tmp_path / "project-dir"))
        )
        assert result == tmp_path / "project-dir", (
            f"expected {tmp_path / 'project-dir'} but got {result!r}"
        )


# ---------------------------------------------------------------------------
# parse_env_compush

# ---------------------------------------------------------------------------


class TestParseEnvCompush:
    """Tests for the compush flag in parse_env()."""

    def test_compush_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POST_TURN_COMPUSH", "1")
        _base, options = hook.parse_env()
        assert options.compush is True, (
            f"expected compush to be True but was {options.compush!r}"
        )

    def test_compush_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POST_TURN_COMPUSH", raising=False)
        _base, options = hook.parse_env()
        assert options.compush is False, (
            f"expected compush to be False but was {options.compush!r}"
        )

    def test_compush_truthy_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POST_TURN_COMPUSH", "yes")
        _base, options = hook.parse_env()
        assert options.compush is True, (
            f"expected compush to be True but was {options.compush!r}"
        )


class TestParseEnvBuildDriver:
    """Tests for build-driver environment parsing."""

    def test_build_driver_defaults_to_auto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("POST_TURN_BUILD_DRIVER", raising=False)
        _base, options = hook.parse_env()
        assert options.build_driver == "auto"

    def test_build_driver_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POST_TURN_BUILD_DRIVER", "netsuke")
        monkeypatch.setenv("POST_TURN_NETSUKE_BIN", "/opt/bin/netsuke")
        monkeypatch.setenv("POST_TURN_MAKE_BIN", "/opt/bin/make")
        _base, options = hook.parse_env()
        assert options.build_driver == "netsuke"
        assert options.netsuke_bin == "/opt/bin/netsuke"
        assert options.make_bin == "/opt/bin/make"


# ---------------------------------------------------------------------------
# run_stop_checks - compush integration

# ---------------------------------------------------------------------------


class TestMain:
    """End-to-end tests for main()."""

    def test_exits_zero_outside_repo(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """main() returns 0 with empty stdout when CWD is not a git repo."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        with (
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch("shutil.which", return_value="/usr/bin/git"),
            mock.patch.object(
                pipeline_mod, "repo_root", return_value=(None, "not a git repository")
            ),
        ):
            rc = hook.main()
        assert rc == 0, f"expected exit 0 but got {rc!r}"
        assert capsys.readouterr().out == "", "expected no output outside repo"

    def test_exits_zero_no_stdin(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """main() returns 0 with empty stdout when stdin is empty."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "nonexistent"))
        with (
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch("shutil.which", return_value="/usr/bin/git"),
            mock.patch.object(
                pipeline_mod, "repo_root", return_value=(None, "not a repo")
            ),
        ):
            rc = hook.main()
        assert rc == 0, f"expected exit 0 but got {rc!r}"
        assert capsys.readouterr().out == "", "expected no output with no stdin"
