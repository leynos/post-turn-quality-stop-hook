"""Exercise GitHub pull request lookup helpers."""

from __future__ import annotations

from post_turn_quality_stop_hook import github as github_mod


class TestParseRemoteUrl:
    """Tests for GitHub remote URL parsing."""

    def test_https_remote(self) -> None:
        """HTTPS remotes are parsed into owner and repository."""
        result = github_mod.parse_remote_url("https://github.com/leynos/example.git")
        assert result == ("leynos", "example")

    def test_ssh_remote(self) -> None:
        """SSH remotes are parsed into owner and repository."""
        result = github_mod.parse_remote_url("git@github.com:leynos/example.git")
        assert result == ("leynos", "example")

    def test_unrecognised_remote_returns_none(self) -> None:
        """Non-GitHub-shaped remotes are ignored."""
        result = github_mod.parse_remote_url("file:///tmp/example")
        assert result is None
