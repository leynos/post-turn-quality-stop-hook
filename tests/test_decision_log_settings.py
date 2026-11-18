"""Settings resolved from configuration, environment and defaults."""

from __future__ import annotations

from pathlib import Path

import pytest
from decision_log_support import (
    CONFIG_RETENTION,
    CORRELATION,
    DEFAULT_RETENTION,
    ENV_RETENTION,
    FALLBACK_RETENTION,
    _log_files,
    _settings,
)

from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.decision_log import (
    InvocationContext,
    make_decision_log,
    parse_retention_days,
    settings_from_env,
)


class TestSettings:
    """Resolve settings from configuration and the environment."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, 14),
            ("", 14),
            ("  ", 14),
            ("not-a-number", 14),
            ("-3", 14),
            ("0", 0),
            ("7", 7),
            (" 21 ", 21),
        ],
    )
    def test_parse_retention_days(self, raw: str | None, expected: int) -> None:
        """Only non-negative integers override the default."""
        assert parse_retention_days(raw, 14) == expected

    def test_defaults_enable_logging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Logging is on by default beneath ``XDG_STATE_HOME``."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))

        settings = settings_from_env()

        assert settings.enabled is True
        assert settings.state_home == tmp_path / "xdg"
        assert settings.retention_days == DEFAULT_RETENTION

    def test_state_home_falls_back_to_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``~/.local/state`` is used when ``XDG_STATE_HOME`` is unset."""
        monkeypatch.delenv("XDG_STATE_HOME")
        # ``Path.home`` reads ``HOME`` on POSIX and ``USERPROFILE`` on Windows.
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

        settings = settings_from_env()

        assert settings.state_home == tmp_path / "home" / ".local" / "state"

    def test_relative_state_home_falls_back_to_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative ``XDG_STATE_HOME`` is ignored, as the XDG spec requires.

        Honouring it would put the log inside the repository that the hook is
        checking, and the uncommitted-changes gate would then block the run on
        the log file itself.
        """
        monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

        settings = settings_from_env()

        assert settings.state_home == tmp_path / "home" / ".local" / "state"

    def test_config_values_apply(self, tmp_path: Path) -> None:
        """Configuration can disable logging and set retention."""
        settings = settings_from_env(
            Config(log_enabled=False, log_retention_days=CONFIG_RETENTION)
        )

        assert settings.enabled is False
        assert settings.retention_days == CONFIG_RETENTION

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1", True),
            ("true", True),
            ("yes", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("no", False),
            ("off", False),
        ],
    )
    def test_environment_toggle_overrides_config(
        self,
        raw: str,
        expected: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``POST_TURN_LOG`` overrides the config value in either direction."""
        monkeypatch.setenv("POST_TURN_LOG", raw)

        settings = settings_from_env(Config(log_enabled=not expected))

        assert settings.enabled is expected

    def test_unrecognized_toggle_keeps_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unparseable toggle is ignored rather than guessed."""
        monkeypatch.setenv("POST_TURN_LOG", "maybe")

        settings = settings_from_env(Config(log_enabled=False))

        assert settings.enabled is False

    def test_environment_retention_overrides_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``POST_TURN_LOG_RETENTION_DAYS`` wins over the config value."""
        monkeypatch.setenv("POST_TURN_LOG_RETENTION_DAYS", str(ENV_RETENTION))

        settings = settings_from_env(Config(log_retention_days=FALLBACK_RETENTION))

        assert settings.retention_days == ENV_RETENTION

    def test_invalid_environment_retention_keeps_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An invalid environment retention falls back to the config value."""
        monkeypatch.setenv("POST_TURN_LOG_RETENTION_DAYS", "soon")

        settings = settings_from_env(Config(log_retention_days=FALLBACK_RETENTION))

        assert settings.retention_days == FALLBACK_RETENTION

    def test_disabled_settings_create_no_log(self, tmp_path: Path) -> None:
        """``make_decision_log`` returns ``None`` when logging is disabled."""
        log = make_decision_log(
            _settings(tmp_path, enabled=False),
            InvocationContext(
                correlation=CORRELATION,
                start_dir=Path("/repo"),
                base_ref="origin/main",
            ),
        )

        assert log is None
        assert _log_files(tmp_path / "state") == []
