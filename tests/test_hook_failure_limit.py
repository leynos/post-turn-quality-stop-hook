"""Exercise main()'s consecutive-failure breaker.

These tests drive ``hook.main`` with its collaborators patched, proving that
the configured limit, and the default limit for unusable configuration, trip
the breaker for the start directory. The breaker policy is unit-tested in
``test_failure_breaker`` and the file store in ``test_streak_store``.
"""

from __future__ import annotations

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
