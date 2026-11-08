"""Exercise CLI entry point: env parsing, main(), cwd resolution."""

from __future__ import annotations

import io
import json
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

STDIN_MUST_NOT_BE_READ = "stdin must not be read for an invalid command line"


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


class _ExplodingStdin:
    """Stand-in for stdin that fails if the hook reads it."""

    def read(self, *_args: object, **_kwargs: object) -> str:
        """Fail the test when the hook reads standard input."""
        raise AssertionError(STDIN_MUST_NOT_BE_READ)


def _run_main_with_state_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state_home: Path,
) -> tuple[int, str, str]:
    """Run ``main`` in a non-repository directory with a chosen state home."""
    _chdir_with_package_stub(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    with (
        mock.patch("sys.argv", ["post-turn-quality-stop-hook"]),
        mock.patch("sys.stdin", io.StringIO("")),
        mock.patch("shutil.which", return_value="/usr/bin/git"),
        mock.patch.object(
            hook, "repo_root", return_value=(None, "not a git repository")
        ),
        mock.patch.object(
            pipeline_mod, "repo_root", return_value=(None, "not a git repository")
        ),
    ):
        rc = hook.main()
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _decision_records(tmp_path: Path) -> list[dict[str, object]]:
    """Read decision-log records written beneath the isolated state home."""
    log_dir = tmp_path / "xdg-state" / "post-turn-quality-stop-hook" / "log"
    return [
        typ.cast("dict[str, object]", json.loads(line))
        for path in sorted(log_dir.glob("hook-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


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
            ("on", True),
            ("false", False),
        ],
    )
    def test_parse_bool_env(self, value: str, expected: bool) -> None:
        """Check parse_bool_env maps each sample string to the expected boolean."""
        assert hook.parse_bool_env(value) is expected


# ---------------------------------------------------------------------------
# parse_max_output

# ---------------------------------------------------------------------------


class TestParseMaxOutput:
    """Tests for parse_max_output()."""

    def test_valid_integer(self) -> None:
        """Check a valid integer string is parsed to its value."""
        result = hook.parse_max_output("5000")
        expected = 5000
        assert result == expected, f"expected {expected} but got {result!r}"

    def test_zero(self) -> None:
        """Check zero is accepted as a valid output limit."""
        result = hook.parse_max_output("0")
        assert result == 0, f"expected 0 but got {result!r}"

    def test_negative_input_returns_default(self) -> None:
        """Check a negative limit falls back to the supplied default."""
        default = 123
        result = hook.parse_max_output("-100", default=default)
        assert result == default, (
            f"expected default {default} for negative input but got {result!r}"
        )

    def test_non_integer_returns_default(self) -> None:
        """Check a non-integer limit falls back to the supplied default."""
        result = hook.parse_max_output("abc", default=999)
        expected = 999
        assert result == expected, f"expected default {expected} but got {result!r}"

    def test_empty_string_returns_default(self) -> None:
        """Check an empty limit falls back to the supplied default."""
        result = hook.parse_max_output("", default=42)
        expected = 42
        assert result == expected, f"expected default {expected} but got {result!r}"


# ---------------------------------------------------------------------------
# parse_hook_input

# ---------------------------------------------------------------------------


class TestParseHookInput:
    """Tests for parse_hook_input()."""

    def test_empty_stdin(self) -> None:
        """Check empty stdin yields an empty mapping."""
        with mock.patch("sys.stdin", io.StringIO("")):
            result = hook.parse_hook_input()
        assert result == {}, f"expected empty dict but got {result!r}"

    def test_valid_json(self) -> None:
        """Check JSON on stdin is parsed into a mapping."""
        with mock.patch("sys.stdin", io.StringIO('{"cwd": "/some/project"}')):
            result = hook.parse_hook_input()
        expected = {"cwd": "/some/project"}
        assert result == expected, f"expected {expected} but got {result!r}"

    def test_invalid_json_returns_empty(self) -> None:
        """Check malformed JSON on stdin yields an empty mapping."""
        with mock.patch("sys.stdin", io.StringIO("not json")):
            result = hook.parse_hook_input()
        assert result == {}, f"expected empty dict for invalid json but got {result!r}"

    def test_correlation_fields_are_kept(self) -> None:
        """Correlation fields survive parsing for the decision log."""
        raw = {
            "cwd": "/project",
            "session_id": "session-9",
            "transcript_path": "/logs/session-9.jsonl",
            "hook_event_name": "Stop",
            "stop_hook_active": True,
        }
        with mock.patch("sys.stdin", io.StringIO(json.dumps(raw))):
            result = hook.parse_hook_input()
        assert result == raw, f"expected {raw} but got {result!r}"

    def test_mistyped_correlation_fields_are_dropped(self) -> None:
        """Only string correlation values and a boolean flag are accepted."""
        raw = {
            "session_id": 7,
            "transcript_path": None,
            "hook_event_name": ["Stop"],
            "stop_hook_active": "yes",
        }
        with mock.patch("sys.stdin", io.StringIO(json.dumps(raw))):
            result = hook.parse_hook_input()
        assert result == {}, f"expected invalid fields dropped but got {result!r}"


# ---------------------------------------------------------------------------
# resolve_start_cwd

# ---------------------------------------------------------------------------


class TestResolveStartCwd:
    """Tests for resolve_start_cwd()."""

    def test_cwd_from_hook_input(self, tmp_path: Path) -> None:
        """Check the cwd in the hook input is used as the start directory."""
        result = hook.resolve_start_cwd(hook.HookInput(cwd=str(tmp_path / "project")))
        assert result == tmp_path / "project", (
            f"expected {tmp_path / 'project'} but got {result!r}"
        )

    def test_claude_project_dir_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Check CLAUDE_PROJECT_DIR is used when the hook input has no cwd."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "claude-project"))
        result = hook.resolve_start_cwd(hook.HookInput())
        assert result == tmp_path / "claude-project", (
            f"expected {tmp_path / 'claude-project'} but got {result!r}"
        )

    def test_falls_back_to_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Check the process working directory is used when nothing else is set."""
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        _chdir_with_package_stub(monkeypatch, tmp_path)
        result = hook.resolve_start_cwd(hook.HookInput())
        assert result == tmp_path, f"expected {tmp_path} but got {result!r}"

    def test_empty_cwd_falls_back_to_project_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Check an empty hook cwd defers to project_dir over the environment."""
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
        """Check POST_TURN_COMPUSH no longer enables the compush option."""
        monkeypatch.setenv("POST_TURN_COMPUSH", "1")
        _base, options = hook.parse_env()
        assert options.compush is False

    def test_compush_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Check the compush option stays disabled when POST_TURN_COMPUSH is unset."""
        monkeypatch.delenv("POST_TURN_COMPUSH", raising=False)
        _base, options = hook.parse_env()
        assert options.compush is False


class TestParseEnvBuildDriver:
    """Tests for build-driver environment parsing."""

    def test_build_driver_defaults_to_auto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Check the build driver defaults to auto when its variable is unset."""
        monkeypatch.delenv("POST_TURN_BUILD_DRIVER", raising=False)
        _base, options = hook.parse_env()
        assert options.build_driver == "auto"

    def test_build_driver_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Check the build driver and tool binary paths come from the environment."""
        monkeypatch.setenv("POST_TURN_BUILD_DRIVER", "netsuke")
        monkeypatch.setenv("POST_TURN_NETSUKE_BIN", "/opt/bin/netsuke")
        monkeypatch.setenv("POST_TURN_MAKE_BIN", "/opt/bin/make")
        _base, options = hook.parse_env()
        assert options.build_driver == "netsuke"
        assert options.netsuke_bin == "/opt/bin/netsuke"
        assert options.make_bin == "/opt/bin/make"

    def test_config_defaults_when_not_passed(self) -> None:
        """Check parse_env uses a default Config when none is passed."""
        _base, options = hook.parse_env()
        assert options.config == Config()

    def test_config_can_be_passed(self) -> None:
        """Check parse_env keeps a Config passed by the caller."""
        config = Config(gate_pr_rebase=False)
        _base, options = hook.parse_env(config)
        assert options.config == config


class TestParseCliArgs:
    """Tests for command-line option parsing."""

    def test_defaults_to_no_config_override(self) -> None:
        """Check no --config flag leaves the config override unset."""
        options = hook.parse_cli_args([])
        assert isinstance(options, hook.CliOptions)
        assert options.config is None

    def test_config_override_path(self, tmp_path: Path) -> None:
        """Check --config stores the given path as the config override."""
        config_path = tmp_path / "config.toml"
        options = hook.parse_cli_args(["--config", str(config_path)])
        assert isinstance(options, hook.CliOptions)
        assert options.config == config_path

    def test_invalid_argument_returns_config_error(self) -> None:
        """Check an unknown argument yields a ConfigError rather than raising."""
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
        """Check main loads the repo-local config into the stop-check options."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".post-turn-quality.toml").write_text("gate_pr_rebase = false\n")
        _chdir_with_package_stub(monkeypatch, repo)
        captured: dict[str, object] = {}

        def fake_run_stop_checks(
            start_cwd: Path,
            base_ref: str,
            options: object,
            *,
            invocation: object = None,
        ) -> int:
            """Record the call arguments and report success."""
            captured["start_cwd"] = start_cwd
            captured["base_ref"] = base_ref
            captured["options"] = options
            captured["invocation"] = invocation
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
        """Check a --config override takes precedence over the repo-local config."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".post-turn-quality.toml").write_text("gate_pr_rebase = false\n")
        override = tmp_path / "override.toml"
        override.write_text("gate_pr_rebase = true\n")
        _chdir_with_package_stub(monkeypatch, repo)
        captured: dict[str, object] = {}

        def fake_run_stop_checks(
            start_cwd: Path,
            base_ref: str,
            options: object,
            *,
            invocation: object = None,
        ) -> int:
            """Record the call arguments and report success."""
            captured["start_cwd"] = start_cwd
            captured["base_ref"] = base_ref
            captured["options"] = options
            captured["invocation"] = invocation
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
        """Check an invalid CLI argument prints an error and exits zero.

        The output names the invalid command line, and the stop checks are not
        run.
        """
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

    def test_invalid_cli_does_not_read_stdin_or_discover_the_repository(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An invalid command line is answered before stdin or Git work."""
        _chdir_with_package_stub(monkeypatch, tmp_path)
        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook", "--unknown"]),
            mock.patch("sys.stdin", _ExplodingStdin()),
            mock.patch.object(
                hook,
                "repo_root",
                side_effect=AssertionError("Git discovery must not run"),
            ),
        ):
            rc = hook.main()

        payload = capsys.readouterr().out
        assert rc == 0, f"expected rc 0 but got {rc!r}"
        assert "Invalid command line" in payload

    def test_finish_record_includes_startup_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The finish record times the work done before the log existed."""
        _chdir_with_package_stub(monkeypatch, tmp_path)
        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook"]),
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch("shutil.which", return_value="/usr/bin/git"),
            mock.patch.object(
                hook, "repo_root", return_value=(None, "not a git repository")
            ),
            mock.patch.object(
                pipeline_mod, "repo_root", return_value=(None, "not a git repository")
            ),
        ):
            rc = hook.main()

        assert rc == 0, f"expected rc 0 but got {rc!r}"
        finish = _decision_records(tmp_path)[-1]
        stages = typ.cast("dict[str, int]", finish["stages"])
        assert "startup" in stages, f"startup must be timed but got {stages!r}"
        assert typ.cast("int", finish["duration_ms"]) >= stages["startup"]

    def test_repo_config_opt_out_writes_no_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repository that disables logging writes no decision-log files."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".post-turn-quality.toml").write_text("log_enabled = false\n")
        _chdir_with_package_stub(monkeypatch, repo)
        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook"]),
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch.object(hook, "repo_root", return_value=(repo, None)),
            mock.patch.object(hook, "run_stop_checks", return_value=0),
        ):
            rc = hook.main()

        assert rc == 0, f"expected rc 0 but got {rc!r}"
        assert _decision_records(tmp_path) == []

    def test_late_config_opt_out_suppresses_later_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config opt-out found after startup stops every later record."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".post-turn-quality.toml").write_text("log_enabled = false\n")
        _chdir_with_package_stub(monkeypatch, tmp_path)
        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook"]),
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch.object(hook, "repo_root", return_value=(repo, None)),
            mock.patch.object(hook, "run_stop_checks", return_value=0),
        ):
            rc = hook.main()

        assert rc == 0, f"expected rc 0 but got {rc!r}"
        records = _decision_records(tmp_path)
        record_types = [record["record_type"] for record in records]
        assert record_types[0] == "start", (
            "the append-only start record remains after the opt-out"
        )
        assert "finish" not in record_types, (
            "an opted-out run must not write a finish record"
        )
        assert all(kind == "progress" for kind in record_types[1:]), (
            "only records written before the opt-out may remain"
        )

    def test_unwritable_log_location_leaves_the_decision_unchanged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An unwritable state home warns but keeps the decision and stdout."""
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")

        writable = _run_main_with_state_home(
            tmp_path, monkeypatch, capsys, tmp_path / "state"
        )
        unwritable = _run_main_with_state_home(
            tmp_path, monkeypatch, capsys, blocker / "state"
        )

        assert unwritable[0] == writable[0], "the exit code must not change"
        assert unwritable[1] == writable[1], "stdout must not change"
        assert "could not write the decision log" in unwritable[2]

    def test_main_records_start_and_finish_with_correlation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run writes joinable start and finish records."""
        _chdir_with_package_stub(monkeypatch, tmp_path)
        raw = {
            "cwd": str(tmp_path),
            "session_id": "session-9",
            "transcript_path": "/logs/session-9.jsonl",
            "hook_event_name": "Stop",
            "stop_hook_active": True,
        }
        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook"]),
            mock.patch("sys.stdin", io.StringIO(json.dumps(raw))),
            mock.patch("shutil.which", return_value="/usr/bin/git"),
            mock.patch.object(
                hook, "repo_root", return_value=(None, "not a git repository")
            ),
            mock.patch.object(
                pipeline_mod, "repo_root", return_value=(None, "not a git repository")
            ),
        ):
            rc = hook.main()

        assert rc == 0, f"expected rc 0 but got {rc!r}"
        records = _decision_records(tmp_path)
        start = records[0]
        assert start["record_type"] == "start"
        assert "session_id" not in start, (
            "the start record is written before the hook input is read"
        )
        finish = records[-1]
        assert finish["record_type"] == "finish"
        assert finish["decision"] == "allow"
        assert finish["run_id"] == start["run_id"]
        assert finish["session_id"] == "session-9"
        assert finish["transcript_path"] == "/logs/session-9.jsonl"
        assert finish["hook_event_name"] == "Stop"
        assert finish["stop_hook_active"] is True

    def test_early_cli_block_is_recorded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A block raised before the pipeline still writes a finish record."""
        _chdir_with_package_stub(monkeypatch, tmp_path)
        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook", "--unknown"]),
            mock.patch("sys.stdin", io.StringIO("")),
        ):
            rc = hook.main()

        payload = capsys.readouterr().out
        assert "Invalid command line" in payload, (
            "standard output keeps the detailed reason"
        )
        assert rc == 0, f"expected rc 0 but got {rc!r}"
        finish = _decision_records(tmp_path)[-1]
        assert finish["record_type"] == "finish"
        assert finish["decision"] == "block"
        assert finish["block_reason"] == "invalid command line or configuration"

    def test_logging_can_be_disabled_from_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``POST_TURN_LOG=0`` writes no decision-log files."""
        _chdir_with_package_stub(monkeypatch, tmp_path)
        monkeypatch.setenv("POST_TURN_LOG", "0")
        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook"]),
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch("shutil.which", return_value="/usr/bin/git"),
            mock.patch.object(
                hook, "repo_root", return_value=(None, "not a git repository")
            ),
            mock.patch.object(
                pipeline_mod, "repo_root", return_value=(None, "not a git repository")
            ),
        ):
            rc = hook.main()

        assert rc == 0, f"expected rc 0 but got {rc!r}"
        assert _decision_records(tmp_path) == []

    def test_log_construction_failure_leaves_the_decision_unchanged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A logging failure warns but never changes the hook decision."""
        _chdir_with_package_stub(monkeypatch, tmp_path)
        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook"]),
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch.object(
                hook, "make_decision_log", side_effect=RuntimeError("no state dir")
            ),
            mock.patch("shutil.which", return_value="/usr/bin/git"),
            mock.patch.object(
                hook, "repo_root", return_value=(None, "not a git repository")
            ),
            mock.patch.object(
                pipeline_mod, "repo_root", return_value=(None, "not a git repository")
            ),
        ):
            rc = hook.main()

        captured = capsys.readouterr()
        assert rc == 0, f"expected exit 0 but got {rc!r}"
        assert captured.out == "", "stdout is reserved for the hook JSON"
        assert "could not create the decision log" in captured.err

    def test_unexpected_exception_is_recorded_then_reraised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An internal error is logged without its environment-bearing text."""
        _chdir_with_package_stub(monkeypatch, tmp_path)
        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook"]),
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch.object(
                hook,
                "run_stop_checks",
                side_effect=RuntimeError("SECRET /home/user/.config"),
            ),
            pytest.raises(RuntimeError, match="SECRET"),
        ):
            hook.main()

        finish = _decision_records(tmp_path)[-1]
        assert finish["record_type"] == "finish"
        assert finish["decision"] == "error"
        assert finish["exit_code"] == 1
        assert finish["error"] == "RuntimeError"
        assert "SECRET" not in json.dumps(finish), (
            "exception text may carry environment values and must not be logged"
        )
