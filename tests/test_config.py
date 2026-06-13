"""Exercise configuration loading."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

import pytest

from post_turn_quality_stop_hook.config import Config, ConfigError, load_config


def test_defaults_when_no_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default config is returned when neither repo nor XDG files exist."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    config = load_config(tmp_path)

    assert config == Config()
    assert config.protected_branches == ("trunk", "main", "release", "master")


def test_repo_local_override_wins_over_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repo-local values override matching XDG values."""
    xdg_home = tmp_path / "xdg"
    xdg_config = xdg_home / "post-turn-quality-stop-hook" / "config.toml"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text("gate_pr_rebase = false\nprimary_remote = 'upstream'\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".post-turn-quality.toml").write_text("gate_pr_rebase = true\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))

    config = load_config(repo)

    assert config.gate_pr_rebase is True
    assert config.primary_remote == "upstream"


def test_xdg_override_used_when_repo_local_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """XDG config applies when the repo-local file is absent."""
    xdg_home = tmp_path / "xdg"
    xdg_config = xdg_home / "post-turn-quality-stop-hook" / "config.toml"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text("gate_uncommitted_changes = false\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))

    config = load_config(repo)

    assert config.gate_uncommitted_changes is False


def test_explicit_override_has_highest_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit config overrides both repo-local and XDG values."""
    xdg_home = tmp_path / "xdg"
    xdg_config = xdg_home / "post-turn-quality-stop-hook" / "config.toml"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text("base_branch_default = 'develop'\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".post-turn-quality.toml").write_text("base_branch_default = 'main'\n")
    override = tmp_path / "override.toml"
    override.write_text("base_branch_default = 'release'\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))

    config = load_config(repo, override=override)

    assert config.base_branch_default == "release"


def test_unknown_key_errors(tmp_path: Path) -> None:
    """Unknown config keys raise an explicit config error."""
    (tmp_path / ".post-turn-quality.toml").write_text("gate_rebsae = false\n")

    with pytest.raises(ConfigError, match="gate_rebsae"):
        load_config(tmp_path)


def test_invalid_type_raises_config_error(tmp_path: Path) -> None:
    """Wrong TOML value types raise a configuration error."""
    (tmp_path / ".post-turn-quality.toml").write_text('gate_quality_checks = "yes"\n')

    with pytest.raises(ConfigError, match="Invalid configuration value"):
        load_config(tmp_path)


def test_protected_branches_are_loaded_from_config(tmp_path: Path) -> None:
    """Protected branch names are loaded through the TOML config mechanism."""
    (tmp_path / ".post-turn-quality.toml").write_text(
        'protected_branches = ["develop", "stable"]\n'
    )

    config = load_config(tmp_path)

    assert config.protected_branches == ("develop", "stable")


def test_invalid_protected_branch_type_raises_config_error(tmp_path: Path) -> None:
    """Protected branch values must be strings."""
    (tmp_path / ".post-turn-quality.toml").write_text(
        'protected_branches = ["main", 123]\n'
    )

    with pytest.raises(ConfigError, match="protected_branches"):
        load_config(tmp_path)
