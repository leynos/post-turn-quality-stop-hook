"""Exercise main()'s consecutive-failure breaker.

These tests drive ``hook.main`` with its collaborators patched, proving that
the configured limit, and the default limit for unusable configuration, trip
the breaker for the start directory. The breaker policy is unit-tested in
``test_failure_breaker`` and the file store in ``test_streak_store``.
"""

from __future__ import annotations

import json
import logging
import typing as typ
from unittest import mock

import pytest

from post_turn_quality_stop_hook import hook
from post_turn_quality_stop_hook.config import Config

if typ.TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("isolated_xdg_state")


class TestMainFailureLimit:
    """main() wraps the stop checks in the consecutive-failure breaker."""

    @staticmethod
    def _block(*_args: object) -> int:
        """Emit a blocking decision as run_stop_checks would."""
        print('{"decision": "block", "reason": "broken"}')
        return 0

    def test_configured_limit_short_circuits_main(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The configured limit trips the breaker for the start directory."""
        config = Config(max_consecutive_failures=1)
        with (
            mock.patch.object(hook, "parse_cli_args", return_value=hook.CliOptions()),
            mock.patch.object(hook, "parse_hook_input", return_value=hook.HookInput()),
            mock.patch.object(hook, "resolve_start_cwd", return_value=tmp_path),
            mock.patch.object(hook, "load_runtime_config", return_value=config),
            mock.patch.object(
                hook, "run_stop_checks", side_effect=self._block
            ) as run_stop_checks,
        ):
            first = hook.main()
            first_out = capsys.readouterr().out
            second = hook.main()
            second_out = capsys.readouterr().out

        assert (first, second) == (0, 0)
        assert '"block"' in first_out
        assert "systemMessage" in second_out
        assert '"block"' not in second_out
        run_stop_checks.assert_called_once()

    def test_config_errors_count_towards_the_default_limit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unloadable config cannot trap the agent in an endless loop."""
        error = hook.ConfigError("bad config")
        outputs: list[str] = []
        with (
            mock.patch.object(hook, "parse_cli_args", return_value=hook.CliOptions()),
            mock.patch.object(hook, "parse_hook_input", return_value=hook.HookInput()),
            mock.patch.object(hook, "resolve_start_cwd", return_value=tmp_path),
            mock.patch.object(hook, "load_runtime_config", return_value=error),
        ):
            for _ in range(Config().max_consecutive_failures + 1):
                hook.main()
                outputs.append(capsys.readouterr().out)

        assert all("bad config" in out for out in outputs[:-1])
        assert "systemMessage" in outputs[-1]

    def test_cli_errors_count_towards_the_default_limit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Invalid CLI arguments block, and count, without loading config."""
        outputs: list[str] = []
        with (
            mock.patch.object(
                hook, "parse_cli_args", return_value=hook.ConfigError("bad cli")
            ),
            mock.patch.object(hook, "parse_hook_input", return_value=hook.HookInput()),
            mock.patch.object(hook, "resolve_start_cwd", return_value=tmp_path),
        ):
            for _ in range(Config().max_consecutive_failures + 1):
                hook.main()
                outputs.append(capsys.readouterr().out)

        earlier = [json.loads(out) for out in outputs[:-1]]
        assert all(p == {"decision": "block", "reason": "bad cli"} for p in earlier)
        assert '"decision"' not in outputs[-1]
        assert "systemMessage" in json.loads(outputs[-1])

    @staticmethod
    def _no_home() -> Path:
        """Fail as ``Path.home()`` does when no home directory is known."""
        message = "Could not determine home directory."
        raise RuntimeError(message)

    def test_an_unresolvable_home_still_reports_the_block(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without a state location the breaker is off, not the checks.

        A CLI error still blocks on every stop, and the warning names the
        failed lookup without a path.
        """
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setattr(hook.Path, "home", self._no_home)
        outputs: list[str] = []
        with (
            mock.patch.object(
                hook, "parse_cli_args", return_value=hook.ConfigError("bad cli")
            ),
            mock.patch.object(hook, "parse_hook_input", return_value=hook.HookInput()),
            mock.patch.object(hook, "resolve_start_cwd", return_value=tmp_path),
            caplog.at_level(logging.WARNING, logger=hook.__name__),
        ):
            for _ in range(Config().max_consecutive_failures + 1):
                assert hook.main() == 0
                outputs.append(capsys.readouterr().out)

        assert all(
            json.loads(out) == {"decision": "block", "reason": "bad cli"}
            for out in outputs
        )
        assert caplog.records[0].getMessage() == (
            "failure streak unavailable: operation=locate error=RuntimeError"
        )

    def test_a_disabled_breaker_never_resolves_state(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With the limit at zero, neither the home directory nor state is used."""
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        home = mock.Mock(side_effect=self._no_home)
        monkeypatch.setattr(hook.Path, "home", home)
        config = Config(max_consecutive_failures=0)
        with (
            mock.patch.object(hook, "parse_cli_args", return_value=hook.CliOptions()),
            mock.patch.object(hook, "parse_hook_input", return_value=hook.HookInput()),
            mock.patch.object(hook, "resolve_start_cwd", return_value=tmp_path),
            mock.patch.object(hook, "load_runtime_config", return_value=config),
            mock.patch.object(hook, "run_stop_checks", side_effect=self._block),
        ):
            for _ in range(Config().max_consecutive_failures + 1):
                hook.main()
                assert '"block"' in capsys.readouterr().out

        home.assert_not_called()
