"""Resolve decision-log settings from configuration and the environment."""

from __future__ import annotations

import dataclasses
import os
import typing as typ
from pathlib import Path

from post_turn_quality_stop_hook.decision_log.constants import (
    DEFAULT_RETENTION_DAYS,
    FALSY_TOKENS,
    LOG_SUBDIR,
    TRUTHY_TOKENS,
)

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook.config import Config


@dataclasses.dataclass(slots=True, frozen=True)
class DecisionLogSettings:
    """Resolved decision-log settings.

    Attributes
    ----------
    enabled
        Whether the hook should write a decision log.
    state_home
        XDG state directory that contains the log directory.
    retention_days
        Number of daily files to retain before pruning older files.

    """

    enabled: bool = True
    state_home: Path = dataclasses.field(
        default_factory=lambda: Path.home() / ".local" / "state"
    )
    retention_days: int = DEFAULT_RETENTION_DAYS


def _state_home_from_env() -> Path:
    """Resolve the XDG state directory from the environment."""
    configured = os.environ.get("XDG_STATE_HOME")
    # The XDG specification says to ignore a relative path. Honouring one would
    # put the log inside the repository under test, where the uncommitted
    # changes gate would then see it.
    if configured and Path(configured).is_absolute():
        return Path(configured)
    return Path.home() / ".local" / "state"


def _env_toggle(name: str) -> bool | None:
    """Read a tri-state boolean environment toggle."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    token = raw.strip().lower()
    if token in TRUTHY_TOKENS:
        return True
    if token in FALSY_TOKENS:
        return False
    return None


def parse_retention_days(raw: str | None, default: int) -> int:
    """Parse a non-negative retention period, falling back when invalid.

    Parameters
    ----------
    raw
        Raw environment value.
    default
        Value used when ``raw`` is absent, empty, non-integer, or negative.

    Returns
    -------
    int
        A non-negative retention period in days.

    """
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def settings_from_env(config: Config | None = None) -> DecisionLogSettings:
    """Resolve decision-log settings from configuration and environment.

    Parameters
    ----------
    config
        Loaded hook configuration, or ``None`` when configuration loading
        failed. Environment overrides still apply in that case.

    Returns
    -------
    DecisionLogSettings
        Settings used to construct the per-invocation log.

    """
    enabled = config.log_enabled if config is not None else True
    toggle = _env_toggle("POST_TURN_LOG")
    if toggle is not None:
        enabled = toggle

    retention = (
        config.log_retention_days if config is not None else DEFAULT_RETENTION_DAYS
    )
    retention = parse_retention_days(
        os.environ.get("POST_TURN_LOG_RETENTION_DAYS"), retention
    )
    return DecisionLogSettings(
        enabled=enabled,
        state_home=_state_home_from_env(),
        retention_days=retention,
    )


def log_directory(state_home: Path) -> Path:
    """Return the decision-log directory beneath an XDG state home."""
    return state_home / LOG_SUBDIR
