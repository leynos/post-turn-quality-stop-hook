"""Exercise CLI entry point: env parsing, main(), cwd resolution."""

from __future__ import annotations

import contextlib
import inspect
import io
import os
import typing as typ
from unittest import mock

if typ.TYPE_CHECKING:
    from pathlib import Path

import pytest
from decision_log_support import _finish_record, _read_records

from post_turn_quality_stop_hook import hook
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.state import StopCheckOptions

# Default output cap from the hook's environment parsing, and the smaller cap
# used to prove that an override is read from the supplied mapping.
DEFAULT_MAX_OUT = 12000
OVERRIDDEN_MAX_OUT = 500

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


# ---------------------------------------------------------------------------
# resolve_start_cwd

# ---------------------------------------------------------------------------


class TestResolveStartCwd:
    """Tests for resolve_start_cwd()."""

    def test_cwd_from_hook_input(self, tmp_path: Path) -> None:
        """Check the cwd in the hook input is used as the start directory."""
        result = hook.resolve_start_cwd(
            hook.HookInput(cwd=str(tmp_path / "project")), tmp_path / "fallback"
        )
        assert result == tmp_path / "project", (
            f"expected {tmp_path / 'project'} but got {result!r}"
        )

    def test_payload_project_dir_wins_over_fallback(self, tmp_path: Path) -> None:
        """Check project_dir in the payload is used before the caller fallback."""
        result = hook.resolve_start_cwd(
            hook.HookInput(project_dir=str(tmp_path / "project-dir")),
            tmp_path / "fallback",
        )
        assert result == tmp_path / "project-dir", (
            f"expected {tmp_path / 'project-dir'} but got {result!r}"
        )

    def test_falls_back_to_the_supplied_directory(self, tmp_path: Path) -> None:
        """Check the caller's fallback is used when the payload names nothing."""
        result = hook.resolve_start_cwd(hook.HookInput(), tmp_path / "fallback")
        assert result == tmp_path / "fallback", (
            f"expected {tmp_path / 'fallback'} but got {result!r}"
        )

    def test_empty_cwd_falls_back_to_project_dir(self, tmp_path: Path) -> None:
        """Check an empty hook cwd defers to project_dir over the fallback."""
        result = hook.resolve_start_cwd(
            hook.HookInput(cwd="", project_dir=str(tmp_path / "project-dir")),
            tmp_path / "fallback",
        )
        assert result == tmp_path / "project-dir", (
            f"expected {tmp_path / 'project-dir'} but got {result!r}"
        )


class TestFallbackStartDir:
    """Tests for the edge's start-directory resolution.

    The working directory is part of the process snapshot the composition edge
    reads once, so these tests inject a snapshot and never patch ``Path.cwd``:
    the start directory is a pure function of the snapshot's fields.
    """

    @staticmethod
    def _snapshot(
        environ: dict[str, str] | None = None, *, cwd: Path | None
    ) -> hook.ProcessSnapshot:
        """Return a snapshot with the supplied environment and directory."""
        return hook.ProcessSnapshot(
            environ=environ or {}, home=hook.Path("/home/test"), cwd=cwd
        )

    def test_configured_project_dir_wins(self, tmp_path: Path) -> None:
        """Check CLAUDE_PROJECT_DIR is used before the snapshot's directory."""
        result = hook._fallback_start_dir(
            self._snapshot(
                {"CLAUDE_PROJECT_DIR": str(tmp_path / "claude-project")},
                cwd=tmp_path,
            )
        )
        assert result == tmp_path / "claude-project", (
            f"expected the configured directory but got {result!r}"
        )

    def test_the_snapshot_directory_is_used_when_unconfigured(
        self, tmp_path: Path
    ) -> None:
        """Check the snapshot's working directory is used when nothing is set."""
        result = hook._fallback_start_dir(self._snapshot(cwd=tmp_path))

        assert result == tmp_path, (
            f"expected the snapshot's directory but got {result!r}"
        )

    def test_the_working_directory_is_not_read_when_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Check Path.cwd is never evaluated when CLAUDE_PROJECT_DIR is set.

        The configured project directory is authoritative, so the function
        takes it from the snapshot without consulting a process global. This
        makes ``Path.cwd`` fail to prove the ordering directly.
        """

        def _fail_cwd() -> Path:
            """Fail if the working directory is read."""
            message = "Path.cwd must not be read when CLAUDE_PROJECT_DIR is set"
            raise AssertionError(message)

        monkeypatch.setattr(hook.Path, "cwd", staticmethod(_fail_cwd))

        result = hook._fallback_start_dir(
            self._snapshot(
                {"CLAUDE_PROJECT_DIR": str(tmp_path / "claude-project")},
                cwd=tmp_path,
            )
        )

        assert result == tmp_path / "claude-project", (
            f"expected the configured directory but got {result!r}"
        )

    def test_a_removed_working_directory_falls_back_to_empty(self) -> None:
        """Check an unreadable snapshot directory yields the empty path."""
        assert hook._fallback_start_dir(self._snapshot(cwd=None)) == hook.Path(), (
            "a removed working directory must not abort a payload-free run"
        )


# ---------------------------------------------------------------------------
# parse_env legacy compush env

# ---------------------------------------------------------------------------


class TestParseEnvLegacyCompush:
    """Tests that the legacy compush env no longer drives options."""

    def test_compush_env_ignored(self) -> None:
        """Check POST_TURN_COMPUSH no longer enables the compush option."""
        _base, options = hook.parse_env(environ={"POST_TURN_COMPUSH": "1"})
        assert options.compush is False

    def test_compush_unset(self) -> None:
        """Check the compush option stays disabled when POST_TURN_COMPUSH is unset."""
        _base, options = hook.parse_env(environ={})
        assert options.compush is False


class TestParseEnvBuildDriver:
    """Tests for build-driver environment parsing."""

    def test_build_driver_defaults_to_auto(self) -> None:
        """Check the build driver defaults to auto when its variable is unset."""
        _base, options = hook.parse_env(environ={})
        assert options.build_driver == "auto"

    def test_build_driver_overrides(self) -> None:
        """Check the build driver and tool binary paths come from the environment."""
        _base, options = hook.parse_env(
            environ={
                "POST_TURN_BUILD_DRIVER": "netsuke",
                "POST_TURN_NETSUKE_BIN": "/opt/bin/netsuke",
                "POST_TURN_MAKE_BIN": "/opt/bin/make",
            }
        )
        assert options.build_driver == "netsuke"
        assert options.netsuke_bin == "/opt/bin/netsuke"
        assert options.make_bin == "/opt/bin/make"

    def test_config_defaults_when_not_passed(self) -> None:
        """Check parse_env uses a default Config when none is passed."""
        _base, options = hook.parse_env(environ={})
        assert options.config == Config()

    def test_config_can_be_passed(self) -> None:
        """Check parse_env keeps a Config passed by the caller."""
        config = Config(gate_pr_rebase=False)
        _base, options = hook.parse_env(config, environ={})
        assert options.config == config


class TestParseEnvInjection:
    """Tests for the injectable environment mapping."""

    def test_the_environment_mapping_is_required(self) -> None:
        """Check callers must supply the snapshot rather than fall back.

        The composition edge reads the process environment once and passes it
        here, so no default may silently read ``os.environ`` again.
        """
        parameter = inspect.signature(hook.parse_env).parameters["environ"]

        assert parameter.default is inspect.Parameter.empty, (
            "parse_env must require its environment snapshot"
        )

    def test_the_supplied_snapshot_is_read(self) -> None:
        """Check the supplied mapping supplies the base ref."""
        base_ref, _options = hook.parse_env(
            environ={"POST_TURN_BASE_REF": "origin/trunk"}
        )

        assert base_ref == "origin/trunk", f"expected origin/trunk but got {base_ref!r}"

    def test_empty_mapping_uses_defaults(self) -> None:
        """An isolated empty mapping never observes the process environment."""
        base_ref, options = hook.parse_env(environ={})

        assert base_ref == "origin/main", f"expected origin/main but got {base_ref!r}"
        assert options.always_fetch is False, (
            f"expected always_fetch False but got {options.always_fetch!r}"
        )
        assert options.max_out == DEFAULT_MAX_OUT, (
            f"expected max_out {DEFAULT_MAX_OUT} but got {options.max_out!r}"
        )
        assert options.build_driver == "auto", (
            f"expected build_driver 'auto' but got {options.build_driver!r}"
        )
        assert options.netsuke_bin == "netsuke", (
            f"expected netsuke_bin 'netsuke' but got {options.netsuke_bin!r}"
        )
        assert options.make_bin == "make", (
            f"expected make_bin 'make' but got {options.make_bin!r}"
        )

    def test_partial_mapping_only_overrides_named_values(self) -> None:
        """Unnamed settings keep their defaults in a partial mapping."""
        base_ref, options = hook.parse_env(
            environ={"POST_TURN_BASE_REF": "origin/trunk"}
        )

        assert base_ref == "origin/trunk", f"expected origin/trunk but got {base_ref!r}"
        assert options.max_out == DEFAULT_MAX_OUT, (
            f"expected max_out {DEFAULT_MAX_OUT} but got {options.max_out!r}"
        )
        assert options.build_driver == "auto", (
            f"expected build_driver 'auto' but got {options.build_driver!r}"
        )

    def test_all_six_overrides_are_read_from_the_mapping(self) -> None:
        """Every documented environment knob comes from the supplied mapping."""
        base_ref, options = hook.parse_env(
            environ={
                "POST_TURN_BASE_REF": "origin/trunk",
                "POST_TURN_ALWAYS_FETCH": "yes",
                "POST_TURN_MAX_OUTPUT_CHARS": "500",
                "POST_TURN_BUILD_DRIVER": "netsuke",
                "POST_TURN_NETSUKE_BIN": "/opt/bin/netsuke",
                "POST_TURN_MAKE_BIN": "/opt/bin/make",
            }
        )

        assert base_ref == "origin/trunk", f"expected origin/trunk but got {base_ref!r}"
        assert options.always_fetch is True, (
            f"expected always_fetch True but got {options.always_fetch!r}"
        )
        assert options.max_out == OVERRIDDEN_MAX_OUT, (
            f"expected max_out {OVERRIDDEN_MAX_OUT} but got {options.max_out!r}"
        )
        assert options.build_driver == "netsuke", (
            f"expected build_driver 'netsuke' but got {options.build_driver!r}"
        )
        assert options.netsuke_bin == "/opt/bin/netsuke", (
            f"expected netsuke_bin '/opt/bin/netsuke' but got {options.netsuke_bin!r}"
        )
        assert options.make_bin == "/opt/bin/make", (
            f"expected make_bin '/opt/bin/make' but got {options.make_bin!r}"
        )

    def test_missing_and_empty_values_remain_distinct(self) -> None:
        """An absent key falls back to its default; an empty key does not."""
        missing_base, missing_options = hook.parse_env(environ={})
        empty_base, empty_options = hook.parse_env(
            environ={
                "POST_TURN_BASE_REF": "",
                "POST_TURN_BUILD_DRIVER": "",
                "POST_TURN_MAX_OUTPUT_CHARS": "",
            }
        )

        assert missing_base == "origin/main", (
            f"expected missing base 'origin/main' but got {missing_base!r}"
        )
        assert empty_base == "", f"expected empty base but got {empty_base!r}"
        assert missing_options.build_driver == "auto", (
            f"expected missing driver 'auto' but got {missing_options.build_driver!r}"
        )
        assert empty_options.build_driver == "", (
            f"expected empty driver but got {empty_options.build_driver!r}"
        )
        assert missing_options.max_out == DEFAULT_MAX_OUT, (
            f"expected missing max_out {DEFAULT_MAX_OUT} "
            f"but got {missing_options.max_out!r}"
        )
        assert empty_options.max_out == DEFAULT_MAX_OUT, (
            f"expected empty max_out {DEFAULT_MAX_OUT} "
            f"but got {empty_options.max_out!r}"
        )

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (" TRUE ", True),
            ("Yes", True),
            (" 1 ", True),
            ("maybe", False),
            ("", False),
        ],
    )
    def test_truthy_tokens_ignore_case_and_surrounding_space(
        self, value: str, expected: bool
    ) -> None:
        """Truthy tokens are matched case-insensitively after trimming."""
        _base, options = hook.parse_env(environ={"POST_TURN_ALWAYS_FETCH": value})
        assert options.always_fetch is expected, (
            f"expected always_fetch {expected!r} for {value!r} "
            f"but got {options.always_fetch!r}"
        )

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("0", 0),
            ("-5", DEFAULT_MAX_OUT),
            ("abc", DEFAULT_MAX_OUT),
            ("", DEFAULT_MAX_OUT),
        ],
    )
    def test_output_limits_accept_zero_and_fall_back_on_invalid_input(
        self, value: str, expected: int
    ) -> None:
        """Zero is valid; negative and malformed limits use the default."""
        _base, options = hook.parse_env(environ={"POST_TURN_MAX_OUTPUT_CHARS": value})
        assert options.max_out == expected, (
            f"expected max_out {expected} for {value!r} but got {options.max_out!r}"
        )

    def test_config_can_be_passed_positionally_or_by_keyword(self) -> None:
        """Both supported config call forms retain the supplied Config."""
        config = Config(gate_pr_rebase=False)

        _base, positional = hook.parse_env(config, environ={})
        _base, keyword = hook.parse_env(config=config, environ={})

        assert positional.config == config, (
            f"expected positional config {config!r} but got {positional.config!r}"
        )
        assert keyword.config == config, (
            f"expected keyword config {config!r} but got {keyword.config!r}"
        )


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
                pipeline_mod,
                "repo_root",
                return_value=(None, HookError("repo_root", "not a git repository")),
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
            upstream_source_for: object,
            **_kwargs: object,
        ) -> int:
            """Record the call arguments and report success."""
            captured["start_cwd"] = start_cwd
            captured["base_ref"] = base_ref
            captured["options"] = options
            captured["upstream_source_for"] = upstream_source_for
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
            upstream_source_for: object,
            **_kwargs: object,
        ) -> int:
            """Record the call arguments and report success."""
            captured["start_cwd"] = start_cwd
            captured["base_ref"] = base_ref
            captured["options"] = options
            captured["upstream_source_for"] = upstream_source_for
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
        run. The block is still recorded through the decision-log run, so its
        ``finish`` record says ``block`` even though standard input is never
        read.
        """
        _chdir_with_package_stub(monkeypatch, tmp_path)
        state_home = tmp_path / "xdg-state"
        stdin = mock.Mock(wraps=io.StringIO("{}"))
        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook", "--unknown"]),
            mock.patch("sys.stdin", stdin),
            mock.patch.object(hook, "run_stop_checks") as mock_run,
        ):
            rc = hook.main()

        payload = capsys.readouterr().out
        assert rc == 0
        assert "Invalid command line" in payload
        mock_run.assert_not_called()
        assert not stdin.read.called, "the payload must not be read on this path"
        # The shared helper fails unless the run wrote exactly one finish
        # record, so a regression that appends both a block and an allow for
        # this run cannot pass this assertion.
        finish = _finish_record(_read_records(state_home))
        assert finish["decision"] == "block", (
            "the early block must still be recorded as a block decision"
        )
        assert finish["exit_code"] == 0
        assert finish["block_reason"] == "invalid command line or configuration"

    def test_invalid_cli_prints_before_the_log_run_is_opened(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The invalid-argument payload does not wait on the log's Git queries.

        Opening the decision-log run can ask Git whether the daily file is
        tracked or ignored. The payload must already be printed by then, so a
        slow or failing Git query cannot delay or replace it.
        """
        _chdir_with_package_stub(monkeypatch, tmp_path)
        printed_before_open: list[str] = []

        @contextlib.contextmanager
        def _recording_run(**_kwargs: object) -> typ.Iterator[object]:
            """Record what had been printed when the log run opened."""

            class _Run:
                """No-op lifecycle for this assertion."""

                recorder = None

                def mark_block(self, _reason: str | None) -> None:
                    """Record nothing."""

            printed_before_open.append(capsys.readouterr().out)
            yield _Run()

        with (
            mock.patch("sys.argv", ["post-turn-quality-stop-hook", "--unknown"]),
            mock.patch.object(hook, "decision_log_run", side_effect=_recording_run),
        ):
            rc = hook.main()

        assert rc == 0
        assert printed_before_open, "the decision-log run must still be opened"
        assert "Invalid command line" in printed_before_open[0], (
            "the block payload must be printed before the log run opens"
        )

    def test_main_calls_parse_env_with_config_positionally(
        self, tmp_path: Path
    ) -> None:
        """Check main passes the loaded config as parse_env's first argument.

        The environment is supplied as the keyword ``environ``, from the one
        snapshot ``main`` read, so a value the process changes mid-run cannot
        split a single invocation's settings in two.
        """
        config = Config(gate_pr_rebase=False)
        options = StopCheckOptions(always_fetch=False, max_out=12000, config=config)
        with (
            mock.patch.object(hook, "parse_cli_args", return_value=hook.CliOptions()),
            mock.patch.object(hook, "parse_hook_input", return_value=hook.HookInput()),
            mock.patch.object(hook, "resolve_start_cwd", return_value=tmp_path),
            mock.patch.object(hook, "load_runtime_config", return_value=config),
            mock.patch.object(
                hook,
                "parse_env",
                return_value=("origin/main", options),
            ) as parse_env,
            mock.patch.object(
                hook, "run_stop_checks", return_value=0
            ) as run_stop_checks,
        ):
            rc = hook.main()

        assert rc == 0, f"expected exit 0 but got {rc!r}"
        args, kwargs = parse_env.call_args
        assert args == (config,), (
            f"expected the config first positionally but got {args!r}"
        )
        assert kwargs["environ"] == os.environ, (
            "expected the process environment snapshot as the environ keyword"
        )
        run_stop_checks.assert_called_once_with(
            tmp_path,
            "origin/main",
            options,
            mock.ANY,
            branch_gate_ports=mock.ANY,
            repository_facts_source=mock.ANY,
            invocation=mock.ANY,
            warn=hook.warn,
        )

    def test_main_loads_configuration_from_the_invocation_snapshot(
        self, tmp_path: Path
    ) -> None:
        """Configuration discovery uses the environment ``main`` captured once.

        The hook waits for standard input before it loads configuration, so a
        process environment that changes in that window must not select a
        different XDG directory than the decision log used.
        """
        snapshot = hook.ProcessSnapshot(
            environ={"XDG_CONFIG_HOME": str(tmp_path / "captured")},
            home=tmp_path / "home",
            cwd=tmp_path,
        )
        options = StopCheckOptions(always_fetch=False, max_out=12000)
        with (
            mock.patch.object(hook, "parse_cli_args", return_value=hook.CliOptions()),
            mock.patch.object(hook, "parse_hook_input", return_value=hook.HookInput()),
            mock.patch.object(hook.ProcessSnapshot, "read", return_value=snapshot),
            mock.patch.object(
                hook, "load_runtime_config", return_value=Config()
            ) as load_config,
            mock.patch.object(hook, "parse_env", return_value=("origin/main", options)),
            mock.patch.object(hook, "run_stop_checks", return_value=0),
        ):
            rc = hook.main()

        assert rc == 0, f"expected exit 0 but got {rc!r}"
        _, kwargs = load_config.call_args
        assert kwargs["environ"] == snapshot.environ, (
            "configuration discovery must read the snapshot's environment"
        )
        assert kwargs["home"] == snapshot.home, (
            "configuration discovery must read the snapshot's home"
        )
        assert kwargs["override"] is None, "no --config was supplied"

    def test_main_resolves_the_fallback_from_the_snapshot(self, tmp_path: Path) -> None:
        """The payload-free start directory comes from the snapshot's working directory.

        ``main`` must not read ``Path.cwd`` a second time after the composition
        edge snapshotted it, so the fallback the payload-free run uses is the
        snapshot's own value.
        """
        snapshot_cwd = tmp_path / "snapshot-cwd"
        process = hook.ProcessSnapshot(environ={}, home=tmp_path, cwd=snapshot_cwd)
        captured: dict[str, object] = {}

        class _NullRun:
            """A no-op lifecycle for the duration of the assertion."""

            recorder = None

            def stage(self, _name: str) -> object:
                """Record no stage and do nothing."""
                return contextlib.nullcontext()

            def set_correlation(self, _fields: object) -> None:
                """Record no correlation fields."""

            def apply_config(self, _config: object) -> None:
                """Apply no configuration to the lifecycle."""

            def finish(self, _exit_code: int) -> None:
                """Deliver no terminal record."""

            def mark_block(self, _reason: str) -> None:
                """Mark no block decision."""

        def fake_run_stop_checks(
            start_cwd: Path,
            _base_ref: str,
            _options: object,
            _upstream_source_for: object,
            **_kwargs: object,
        ) -> int:
            """Record the start directory the pipeline received."""
            captured["start_cwd"] = start_cwd
            return 0

        with (
            mock.patch.object(hook.ProcessSnapshot, "read", return_value=process),
            mock.patch.object(
                hook,
                "decision_log_run",
                return_value=contextlib.nullcontext(_NullRun()),
            ),
            mock.patch.object(hook, "parse_cli_args", return_value=hook.CliOptions()),
            mock.patch.object(hook, "parse_hook_input", return_value=hook.HookInput()),
            mock.patch.object(hook, "load_runtime_config", return_value=Config()),
            mock.patch.object(
                hook,
                "parse_env",
                return_value=(
                    "origin/main",
                    StopCheckOptions(always_fetch=False, max_out=12000),
                ),
            ),
            mock.patch.object(
                hook, "run_stop_checks", side_effect=fake_run_stop_checks
            ),
        ):
            rc = hook.main()

        assert rc == 0, f"expected exit 0 but got {rc!r}"
        assert captured["start_cwd"] == snapshot_cwd, (
            "the payload-free fallback must come from the snapshot,"
            f" but got {captured['start_cwd']!r}"
        )

    def test_main_reads_the_process_environment_once(self, tmp_path: Path) -> None:
        """Check main snapshots the environment once and shares it.

        The review of the decision-log branch asked that the base ref come from
        the same snapshot the rest of the invocation reads, so a value the
        process changes mid-run cannot split one run's settings in two. This
        replaces the process environment with one whose ``POST_TURN_BASE_REF``
        differs from the ambient value, and asserts both the base ref the log
        received and the mapping ``parse_env`` was given come from that one
        snapshot.
        """
        snapshot_environ = {"POST_TURN_BASE_REF": "origin/snapshot"}
        process = hook.ProcessSnapshot(
            environ=snapshot_environ, home=tmp_path, cwd=tmp_path
        )
        captured: dict[str, object] = {}

        class _NullRun:
            """A no-op lifecycle for the duration of the assertion."""

            recorder = None

            def stage(self, _name: str) -> object:
                """Record no stage and do nothing."""
                return contextlib.nullcontext()

            def set_correlation(self, _fields: object) -> None:
                """Record no correlation fields."""

            def apply_config(self, _config: object) -> None:
                """Apply no configuration to the lifecycle."""

            def finish(self, _exit_code: int) -> None:
                """Deliver no terminal record."""

            def mark_block(self, _reason: str) -> None:
                """Mark no block decision."""

        @contextlib.contextmanager
        def _capture_log_run(**kwargs: object) -> typ.Iterator[_NullRun]:
            """Record the base ref the log lifecycle was given."""
            captured["base_ref"] = kwargs["base_ref"]
            captured["process"] = kwargs["process"]
            yield _NullRun()

        with (
            mock.patch.object(
                hook.ProcessSnapshot, "read", return_value=process
            ) as snapshot,
            mock.patch.object(hook, "decision_log_run", side_effect=_capture_log_run),
            mock.patch.object(hook, "parse_cli_args", return_value=hook.CliOptions()),
            mock.patch.object(hook, "parse_hook_input", return_value=hook.HookInput()),
            mock.patch.object(hook, "resolve_start_cwd", return_value=tmp_path),
            mock.patch.object(hook, "load_runtime_config", return_value=Config()),
            mock.patch.object(
                hook,
                "parse_env",
                return_value=(
                    "origin/snapshot",
                    StopCheckOptions(always_fetch=False, max_out=12000),
                ),
            ) as parse_env,
            mock.patch.object(hook, "run_stop_checks", return_value=0),
        ):
            hook.main()

        snapshot.assert_called_once_with()
        assert captured["base_ref"] == "origin/snapshot", (
            f"expected the snapshot's base ref but got {captured['base_ref']!r}"
        )
        assert captured["process"] is process, (
            "the one snapshot must be shared with the log lifecycle"
        )
        _args, kwargs = parse_env.call_args
        assert kwargs["environ"] is snapshot_environ, (
            "parse_env must read the snapshot, not the live process environment"
        )

    def test_main_passes_environment_derived_settings(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Check main forwards options derived from the process environment."""
        monkeypatch.setenv("POST_TURN_BASE_REF", "origin/trunk")
        monkeypatch.setenv("POST_TURN_ALWAYS_FETCH", "1")
        monkeypatch.setenv("POST_TURN_MAX_OUTPUT_CHARS", "500")
        monkeypatch.setenv("POST_TURN_BUILD_DRIVER", "netsuke")
        monkeypatch.setenv("POST_TURN_NETSUKE_BIN", "/opt/bin/netsuke")
        monkeypatch.setenv("POST_TURN_MAKE_BIN", "/opt/bin/make")
        config = Config(gate_pr_rebase=False)
        captured: dict[str, object] = {}

        def fake_run_stop_checks(
            start_cwd: Path,
            base_ref: str,
            options: object,
            upstream_source_for: object,
            **_kwargs: object,
        ) -> int:
            """Record the call arguments and report success."""
            captured["start_cwd"] = start_cwd
            captured["base_ref"] = base_ref
            captured["options"] = options
            return 0

        with (
            mock.patch.object(hook, "parse_cli_args", return_value=hook.CliOptions()),
            mock.patch.object(hook, "parse_hook_input", return_value=hook.HookInput()),
            mock.patch.object(hook, "resolve_start_cwd", return_value=tmp_path),
            mock.patch.object(hook, "load_runtime_config", return_value=config),
            mock.patch.object(
                hook, "run_stop_checks", side_effect=fake_run_stop_checks
            ),
        ):
            rc = hook.main()

        assert rc == 0, f"expected exit 0 but got {rc!r}"
        assert captured["start_cwd"] == tmp_path, (
            f"expected start_cwd {tmp_path!r} but got {captured['start_cwd']!r}"
        )
        assert captured["base_ref"] == "origin/trunk", (
            f"expected origin/trunk but got {captured['base_ref']!r}"
        )
        options = captured["options"]
        assert isinstance(options, StopCheckOptions), (
            f"expected StopCheckOptions but got {options!r}"
        )
        assert options.always_fetch is True, (
            f"expected always_fetch True but got {options.always_fetch!r}"
        )
        assert options.max_out == OVERRIDDEN_MAX_OUT, (
            f"expected max_out {OVERRIDDEN_MAX_OUT} but got {options.max_out!r}"
        )
        assert options.build_driver == "netsuke", (
            f"expected build_driver 'netsuke' but got {options.build_driver!r}"
        )
        assert options.netsuke_bin == "/opt/bin/netsuke", (
            f"expected netsuke_bin '/opt/bin/netsuke' but got {options.netsuke_bin!r}"
        )
        assert options.make_bin == "/opt/bin/make", (
            f"expected make_bin '/opt/bin/make' but got {options.make_bin!r}"
        )
        assert options.config == config, (
            f"expected the loaded config but got {options.config!r}"
        )


class TestBlockPayloadEmission:
    """Both orchestration-level blocks go through the shared emitter.

    The payload shape is declared once in ``formatting``. These tests patch
    that emitter and prove the config-error and early CLI paths reach it
    rather than assembling JSON in ``hook``.
    """

    def test_the_config_error_uses_the_shared_emitter(self, tmp_path: Path) -> None:
        """A configuration failure is emitted through ``formatting``."""
        error = hook.ConfigError("could not read the configuration")
        with (
            mock.patch.object(hook, "parse_cli_args", return_value=hook.CliOptions()),
            mock.patch.object(hook, "parse_hook_input", return_value=hook.HookInput()),
            mock.patch.object(hook, "resolve_start_cwd", return_value=tmp_path),
            mock.patch.object(hook, "load_runtime_config", return_value=error),
            mock.patch.object(hook, "emit_block_payload") as emit,
            mock.patch.object(hook, "run_stop_checks") as run_stop_checks,
        ):
            rc = hook.main()

        assert rc == 0, f"expected exit 0 but got {rc!r}"
        emit.assert_called_once_with(str(error))
        run_stop_checks.assert_not_called()

    def test_the_early_cli_block_uses_the_shared_emitter(self) -> None:
        """An invalid command line is emitted through ``formatting``."""
        error = hook.ConfigError("Invalid command line: --unknown")

        class _NullRun:
            """A no-op lifecycle for the duration of the assertion."""

            def mark_block(self, _reason: str) -> None:
                """Record nothing."""

        with (
            mock.patch.object(hook, "parse_cli_args", return_value=error),
            mock.patch.object(hook, "emit_block_payload") as emit,
            mock.patch.object(
                hook,
                "decision_log_run",
                return_value=contextlib.nullcontext(_NullRun()),
            ),
        ):
            rc = hook.main()

        assert rc == 0, f"expected exit 0 but got {rc!r}"
        emit.assert_called_once_with(str(error), flush=True)
