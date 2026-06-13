"""Configuration loading for post-turn quality stop-hook gates."""

from __future__ import annotations

import dataclasses
import os
import tomllib
import typing as typ
from pathlib import Path

CONFIG_FILENAME = ".post-turn-quality.toml"
XDG_CONFIG_SUBPATH = Path("post-turn-quality-stop-hook") / "config.toml"
DEFAULT_PROTECTED_BRANCHES = ("trunk", "main", "release", "master")


@dataclasses.dataclass(slots=True, frozen=True)
class Config:
    """Merged configuration for stop-hook gates."""

    gate_quality_checks: bool = True
    gate_uncommitted_changes: bool = True
    gate_unpushed_commits: bool = True
    gate_pr_rebase: bool = True
    primary_remote: str | None = None
    base_branch_default: str = "main"
    protected_branches: tuple[str, ...] = DEFAULT_PROTECTED_BRANCHES
    github_timeout_seconds: float = 3.0


class ConfigError(ValueError):
    """Raised when a configuration file cannot be accepted."""


def load_config(repo_root: Path, *, override: Path | None = None) -> Config:
    """Load stop-hook configuration from defaults, XDG, repo, and override files."""
    merged = dataclasses.asdict(Config())
    for path in _candidate_paths(repo_root, override=override):
        if path.exists():
            merged.update(_read_config_file(path))
    _validate_value_types(merged)
    _normalise_values(merged)
    try:
        return Config(**merged)
    except (TypeError, ValueError) as err:
        message = "invalid configuration: failed to construct Config"
        raise ConfigError(message) from err


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


def _validate_value_types(data: dict[str, object]) -> None:
    bool_fields = {
        "gate_quality_checks",
        "gate_uncommitted_changes",
        "gate_unpushed_commits",
        "gate_pr_rebase",
    }
    for field_name in bool_fields:
        if not isinstance(data[field_name], bool):
            message = f"Invalid configuration value for {field_name}"
            raise ConfigError(message)

    primary_remote = data["primary_remote"]
    if primary_remote is not None and not isinstance(primary_remote, str):
        message = "Invalid configuration value for primary_remote"
        raise ConfigError(message)
    if not isinstance(data["base_branch_default"], str):
        message = "Invalid configuration value for base_branch_default"
        raise ConfigError(message)
    protected_branches = data["protected_branches"]
    if not isinstance(protected_branches, list | tuple):
        message = "Invalid configuration value for protected_branches"
        raise ConfigError(message)
    if not all(isinstance(branch, str) for branch in protected_branches):
        message = "Invalid configuration value for protected_branches"
        raise ConfigError(message)
    timeout = data["github_timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        message = "Invalid configuration value for github_timeout_seconds"
        raise ConfigError(message)


def _normalise_values(data: dict[str, object]) -> None:
    protected_branches = data["protected_branches"]
    data["protected_branches"] = tuple(
        typ.cast("list[str] | tuple[str, ...]", protected_branches)
    )
