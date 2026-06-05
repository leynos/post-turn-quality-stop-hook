"""Exercise primary remote resolution and collected Git facts."""

from __future__ import annotations

import subprocess  # noqa: S404
from pathlib import Path
from unittest import mock

from post_turn_quality_stop_hook import git as git_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.git_facts import collect_git_facts


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["unit-test"], returncode=returncode, stdout=stdout, stderr=stderr
    )


REPO = Path("/fake/repo")


class TestPrimaryRemoteName:
    """Tests for primary_remote_name()."""

    def test_configured_remote_wins(self) -> None:
        """Configured remote is selected when present."""
        with mock.patch.object(
            git_mod, "run", return_value=_completed(0, "origin\nupstream\n")
        ):
            remote, err = git_mod.primary_remote_name(REPO, "upstream")
        assert remote == "upstream"
        assert err is None

    def test_missing_configured_remote_returns_none(self) -> None:
        """Missing configured remote is unavailable rather than fatal."""
        with mock.patch.object(git_mod, "run", return_value=_completed(0, "origin\n")):
            remote, err = git_mod.primary_remote_name(REPO, "upstream")
        assert remote is None
        assert err is None

    def test_origin_wins_when_unconfigured(self) -> None:
        """Origin is selected by default when present."""
        with mock.patch.object(
            git_mod, "run", return_value=_completed(0, "fork\norigin\n")
        ):
            remote, err = git_mod.primary_remote_name(REPO, None)
        assert remote == "origin"
        assert err is None

    def test_first_remote_used_when_origin_absent(self) -> None:
        """Lexicographically first remote is selected when origin is absent."""
        with mock.patch.object(
            git_mod, "run", return_value=_completed(0, "zeta\nalpha\n")
        ):
            remote, err = git_mod.primary_remote_name(REPO, None)
        assert remote == "alpha"
        assert err is None

    def test_no_remotes_returns_none(self) -> None:
        """A repository with no remotes has no primary remote."""
        with mock.patch.object(git_mod, "run", return_value=_completed(0, "")):
            remote, err = git_mod.primary_remote_name(REPO, None)
        assert remote is None
        assert err is None


def test_collect_git_facts_uses_upstream_base() -> None:
    """Collected facts include primary remote, upstream, merge-base, and style."""
    with (
        mock.patch(
            "post_turn_quality_stop_hook.git_facts.primary_remote_name",
            return_value=("origin", None),
        ),
        mock.patch(
            "post_turn_quality_stop_hook.git_facts.get_upstream_ref",
            return_value=("origin/feature", None),
        ),
        mock.patch(
            "post_turn_quality_stop_hook.git_facts.merge_base",
            return_value=("abc123", None),
        ) as mock_merge_base,
        mock.patch(
            "post_turn_quality_stop_hook.git_facts.is_three_way_merge_configured",
            return_value=True,
        ),
    ):
        facts = collect_git_facts(REPO, Config())

    assert facts.primary_remote == "origin"
    assert facts.upstream_ref == "origin/feature"
    assert facts.local_base_commit == "abc123"
    assert facts.three_way_merge_is_configured is True
    mock_merge_base.assert_called_once_with(REPO, "origin/feature")


def test_collect_git_facts_without_primary_remote_does_not_raise() -> None:
    """Absence of a primary remote leaves optional facts unset."""
    with (
        mock.patch(
            "post_turn_quality_stop_hook.git_facts.primary_remote_name",
            return_value=(None, None),
        ),
        mock.patch(
            "post_turn_quality_stop_hook.git_facts.get_upstream_ref",
            return_value=(None, "no upstream"),
        ),
        mock.patch(
            "post_turn_quality_stop_hook.git_facts.is_three_way_merge_configured",
            return_value=False,
        ),
    ):
        facts = collect_git_facts(REPO, Config())

    assert facts.primary_remote is None
    assert facts.upstream_ref is None
    assert facts.local_base_commit is None
