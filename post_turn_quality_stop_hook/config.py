"""Configuration loading for post-turn quality stop-hook gates."""

from __future__ import annotations

import dataclasses
import os
import tomllib
import typing as typ
from pathlib import Path

import cyclopts

CONFIG_FILENAME = ".post-turn-quality.toml"
XDG_CONFIG_SUBPATH = Path("post-turn-quality-stop-hook") / "config.toml"


@dataclasses.dataclass(slots=True, frozen=True)
class Config:
    """Merged configuration for stop-hook gates."""

    gate_quality_checks: bool = True
    gate_uncommitted_changes: bool = True
    gate_unpushed_commits: bool = True
    gate_pr_rebase: bool = True
    primary_remote: str | None = None
    base_branch_default: str = "main"
    github_timeout_seconds: float = 3.0


class ConfigError(ValueError):
    """Raised when a configuration file cannot be accepted."""


def load_config(repo_root: Path, *, override: Path | None = None) -> Config:
    """Load stop-hook configuration from defaults, XDG, repo, and override files."""
    merged = dataclasses.asdict(Config())
    for path in _candidate_paths(repo_root, override=override):
        if path.exists():
            merged.update(_read_config_file(path))
    return Config(**merged)


def _candidate_paths(repo_root: Path, *, override: Path | None) -> list[Path]:
    paths = [_xdg_config_path(), repo_root / CONFIG_FILENAME]
    if override is not None:
        paths.append(override)
    return paths


def _xdg_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / XDG_CONFIG_SUBPATH
    return Path.home() / ".config" / XDG_CONFIG_SUBPATH


def _read_config_file(path: Path) -> dict[str, object]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        message = f"Invalid configuration file {path}: {exc}"
        raise ConfigError(message) from exc

    if not isinstance(data, dict):
        message = f"Configuration file {path} did not contain a TOML table"
        raise ConfigError(message)

    _validate_keys(data, path)
    return typ.cast("dict[str, object]", data)


def _validate_keys(data: dict[str, object], path: Path) -> None:
    allowed_keys = {field.name for field in dataclasses.fields(Config)}
    unknown_keys = sorted(set(data) - allowed_keys)
    if unknown_keys:
        formatted = ", ".join(unknown_keys)
        message = f"Unknown configuration key(s) in {path}: {formatted}"
        raise ConfigError(message)


def cyclopts_available() -> bool:
    """Return whether the cyclopts dependency is importable.

    Milestone 1 adds the runtime dependency before the CLI wrapper in
    Milestone 6 consumes it directly. Keeping this tiny probe makes that
    dependency explicit and testable without inventing a premature CLI layer.
    """
    return cyclopts.__name__ == "cyclopts"
