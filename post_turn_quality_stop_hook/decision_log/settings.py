"""Resolve decision-log settings from configuration and the environment."""

from __future__ import annotations

import dataclasses
import typing as typ
from pathlib import Path

from post_turn_quality_stop_hook.decision_log.constants import (
    DEFAULT_RETENTION_DAYS,
    FALSY_TOKENS,
    LOG_SUBDIR,
    TRUTHY_TOKENS,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from post_turn_quality_stop_hook.config import Config


@dataclasses.dataclass(slots=True, frozen=True)
class DecisionLogSettings:
    """Resolved decision-log settings.

    Attributes
    ----------
    state_home
        XDG state directory that contains the log directory. It is required:
        the composition edge resolves it from the process environment and home
        it read once, so no caller can silently fall back to whatever home the
        running process happens to have.
    enabled
        Whether the hook should write a decision log.
    retention_days
        Number of daily files to retain before pruning older files.

    """

    state_home: Path
    enabled: bool = True
    retention_days: int = DEFAULT_RETENTION_DAYS


def _state_home_from_env(environ: cabc.Mapping[str, str], home: Path) -> Path:
    """Resolve the XDG state directory from ``environ`` and ``home``."""
    configured = environ.get("XDG_STATE_HOME")
    # The XDG specification says to ignore a relative path. Honouring one would
    # put the log inside the repository under test, where the uncommitted
    # changes gate would then see it.
    if configured and Path(configured).is_absolute():
        return Path(configured)
    return home / ".local" / "state"


def _env_toggle(environ: cabc.Mapping[str, str], name: str) -> bool | None:
    """Read a tri-state boolean environment toggle."""
    raw = environ.get(name)
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


def settings_from_env(
    config: Config | None = None,
    *,
    environ: cabc.Mapping[str, str],
    home: Path,
) -> DecisionLogSettings:
    """Resolve decision-log settings from configuration and environment.

    Parameters
    ----------
    config
        Loaded hook configuration, or ``None`` when configuration loading
        failed. Environment overrides still apply in that case.
    environ
        Environment mapping to read. The composition edge supplies the process
        environment it read once, so settings resolution has no hidden global
        dependency and a test can supply one without patching the process.
    home
        Home directory used when ``XDG_STATE_HOME`` is absent or relative. The
        composition edge supplies it from the same snapshot.

    Returns
    -------
    DecisionLogSettings
        Settings used to construct the per-invocation log.

    """
    # Copy into locals so every lookup uses the same snapshot, even if the
    # caller passes a live mapping that another thread could change.
    environment = environ
    enabled = config.log_enabled if config is not None else True
    toggle = _env_toggle(environment, "POST_TURN_LOG")
    if toggle is not None:
        enabled = toggle

    retention = (
        config.log_retention_days if config is not None else DEFAULT_RETENTION_DAYS
    )
    retention = parse_retention_days(
        environment.get("POST_TURN_LOG_RETENTION_DAYS"), retention
    )
    return DecisionLogSettings(
        state_home=_state_home_from_env(environment, home),
        enabled=enabled,
        retention_days=retention,
    )


def log_directory(state_home: Path) -> Path:
    """Return the decision-log directory beneath an XDG state home."""
    return state_home / LOG_SUBDIR
