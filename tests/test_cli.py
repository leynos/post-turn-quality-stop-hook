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
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.state import StopCheckOptions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chdir_with_package_stub(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Change directory to ``target``, creating a stand-in package dir.

    mutmut's trampoline resolves the configured ``source_paths`` relative
    to the current working directory with ``strict=True``, so mutation
    runs crash in any test that changes directory to a location without a
    ``post_turn_quality_stop_hook`` entry. The empty stand-in directory
    keeps that resolution valid without affecting the behaviour under
    test.
    """
    (target / "post_turn_quality_stop_hook").mkdir(exist_ok=True)
    monkeypatch.chdir(target)


# ---------------------------------------------------------------------------
# parse_bool_env

# ---------------------------------------------------------------------------


class TestParseBoolEnv:
    """Tests for parse_bool_env()."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", False),
            ("0", False),
            ("1", True),
            ("true", True),
            ("yes", True),
            ("false", False),
        ],
    )
    def test_parse_bool_env(self, value: str, expected: bool) -> None:
        assert hook.parse_bool_env(value) is expected


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
        _chdir_with_package_stub(monkeypatch, tmp_path)
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
# parse_env legacy compush env

# ---------------------------------------------------------------------------


class TestParseEnvLegacyCompush:
    """Tests that the legacy compush env no longer drives options."""

    def test_compush_env_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POST_TURN_COMPUSH", "1")
        _base, options = hook.parse_env()
        assert options.compush is False

    def test_compush_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POST_TURN_COMPUSH", raising=False)
        _base, options = hook.parse_env()
        assert options.compush is False


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

    def test_config_defaults_when_not_passed(self) -> None:
        _base, options = hook.parse_env()
        assert options.config == Config()

    def test_config_can_be_passed(self) -> None:
        config = Config(gate_pr_rebase=False)
        _base, options = hook.parse_env(config)
        assert options.config == config


class TestParseCliArgs:
    """Tests for command-line option parsing."""

    def test_defaults_to_no_config_override(self) -> None:
        options = hook.parse_cli_args([])
        assert isinstance(options, hook.CliOptions)
        assert options.config is None

    def test_config_override_path(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        options = hook.parse_cli_args(["--config", str(config_path)])
        assert isinstance(options, hook.CliOptions)
        assert options.config == config_path

    def test_invalid_argument_returns_config_error(self) -> None:
        result = hook.parse_cli_args(["--unknown"])
        assert not isinstance(result, hook.CliOptions)
        assert "Invalid command line" in str(result)


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
        _chdir_with_package_stub(monkeypatch, tmp_path)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook"]),
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
            mock.patch("sys.argv", ["post-turn-quality-stop-hook"]),
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch("shutil.which", return_value="/usr/bin/git"),
            mock.patch.object(
                pipeline_mod, "repo_root", return_value=(None, "not a repo")
            ),
        ):
            rc = hook.main()
        assert rc == 0, f"expected exit 0 but got {rc!r}"
        assert capsys.readouterr().out == "", "expected no output with no stdin"

    def test_loads_repo_local_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".post-turn-quality.toml").write_text("gate_pr_rebase = false\n")
        _chdir_with_package_stub(monkeypatch, repo)
        captured: dict[str, object] = {}

        def fake_run_stop_checks(
            start_cwd: Path, base_ref: str, options: object
        ) -> int:
            captured["start_cwd"] = start_cwd
            captured["base_ref"] = base_ref
            captured["options"] = options
            return 0

        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook"]),
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch.object(hook, "repo_root", return_value=(repo, None)),
            mock.patch.object(
                hook, "run_stop_checks", side_effect=fake_run_stop_checks
            ),
        ):
            rc = hook.main()

        assert rc == 0
        options = captured["options"]
        assert isinstance(options, StopCheckOptions)
        assert options.config.gate_pr_rebase is False

    def test_config_cli_override_wins_over_repo_local(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".post-turn-quality.toml").write_text("gate_pr_rebase = false\n")
        override = tmp_path / "override.toml"
        override.write_text("gate_pr_rebase = true\n")
        _chdir_with_package_stub(monkeypatch, repo)
        captured: dict[str, object] = {}

        def fake_run_stop_checks(
            start_cwd: Path, base_ref: str, options: object
        ) -> int:
            captured["start_cwd"] = start_cwd
            captured["base_ref"] = base_ref
            captured["options"] = options
            return 0

        with (
            mock.patch(
                "sys.argv", ["post-turn-quality-stop-hook", "--config", str(override)]
            ),
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch.object(hook, "repo_root", return_value=(repo, None)),
            mock.patch.object(
                hook, "run_stop_checks", side_effect=fake_run_stop_checks
            ),
        ):
            rc = hook.main()

        assert rc == 0
        options = captured["options"]
        assert isinstance(options, StopCheckOptions)
        assert options.config.gate_pr_rebase is True

    def test_invalid_cli_blocks_with_zero_exit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _chdir_with_package_stub(monkeypatch, tmp_path)
        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook", "--unknown"]),
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch.object(hook, "run_stop_checks") as mock_run,
        ):
            rc = hook.main()

        payload = capsys.readouterr().out
        assert rc == 0
        assert "Invalid command line" in payload
        mock_run.assert_not_called()
