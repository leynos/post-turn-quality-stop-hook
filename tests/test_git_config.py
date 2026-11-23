"""Exercise Git configuration reads and upstream resolution."""

from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path
from unittest import mock

from post_turn_quality_stop_hook import git as git_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess with the given return code and output."""
    return subprocess.CompletedProcess(
        args=["unit-test"], returncode=returncode, stdout=stdout, stderr=stderr
    )


REPO = Path("/fake/repo")
URL_REMOTE = "https://example.invalid/hub.git"


class TestGitConfigGet:
    """Tests for git_config_get()."""

    def test_returns_stripped_value(self) -> None:
        """A configured value is returned without trailing whitespace."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="origin\n")
            value, err = git_mod.git_config_get(REPO, "branch.feature.remote")
        assert value == "origin", f"expected origin but got {value!r}"
        assert err is None, f"expected no error but got {err!r}"
        mock_run.assert_called_once_with(
            ["git", "config", "--get", "branch.feature.remote"], REPO
        )

    def test_unset_key_returns_no_value_and_no_error(self) -> None:
        """``git config --get`` exits 1 for an unset key, not an error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(1)
            value, err = git_mod.git_config_get(REPO, "branch.feature.remote")
        assert value is None, f"expected no value but got {value!r}"
        assert err is None, f"expected no error but got {err!r}"

    def test_empty_value_returns_none(self) -> None:
        """An empty value is treated as unset."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="\n")
            value, err = git_mod.git_config_get(REPO, "branch.feature.remote")
        assert value is None, f"expected no value but got {value!r}"
        assert err is None, f"expected no error but got {err!r}"

    def test_read_failure_returns_error(self) -> None:
        """A config read failure is surfaced, not swallowed."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(128, stderr="fatal: bad config")
            value, err = git_mod.git_config_get(REPO, "branch.feature.remote")
        assert value is None, f"expected no value but got {value!r}"
        assert "fatal: bad config" in (err or ""), (
            f"expected fatal error in message but got {err!r}"
        )


class TestGitConfigGetFirst:
    """Tests for git_config_get_first()."""

    def test_returns_the_first_of_several_values(self) -> None:
        """Git's ``@{u}`` uses the first merge entry, not the last."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(
                0, stdout="refs/heads/feature\nrefs/heads/main\n"
            )
            value, err = git_mod.git_config_get_first(REPO, "branch.feature.merge")
        assert value == "refs/heads/feature", (
            f"expected the first value but got {value!r}"
        )
        assert err is None, f"expected no error but got {err!r}"
        mock_run.assert_called_once_with(
            ["git", "config", "--get-all", "branch.feature.merge"], REPO
        )

    def test_unset_key_returns_no_value_and_no_error(self) -> None:
        """An unset multi-valued key is not an error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(1)
            value, err = git_mod.git_config_get_first(REPO, "branch.feature.merge")
        assert value is None, f"expected no value but got {value!r}"
        assert err is None, f"expected no error but got {err!r}"

    def test_empty_output_returns_none(self) -> None:
        """Blank output is treated as an unset key."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="\n\n")
            value, err = git_mod.git_config_get_first(REPO, "branch.feature.merge")
        assert value is None, f"expected no value but got {value!r}"
        assert err is None, f"expected no error but got {err!r}"

    def test_read_failure_returns_error(self) -> None:
        """A multi-valued config read failure is surfaced."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(128, stderr="fatal: bad config")
            value, err = git_mod.git_config_get_first(REPO, "branch.feature.merge")
        assert value is None, f"expected no value but got {value!r}"
        assert "fatal: bad config" in (err or ""), (
            f"expected fatal error in message but got {err!r}"
        )


# ---------------------------------------------------------------------------
# current_branch
# ---------------------------------------------------------------------------


class TestResolveUpstream:
    """Tests for resolve_upstream()."""

    def test_returns_named_remote_and_branch(self) -> None:
        """A named remote and its ref are returned fully qualified."""
        with (
            mock.patch.object(
                git_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(git_mod, "git_config_get", return_value=("origin", None)),
            mock.patch.object(
                git_mod,
                "git_config_get_first",
                return_value=("refs/heads/feature", None),
            ),
        ):
            remote, branch, err = git_mod.resolve_upstream(REPO)
        assert (remote, branch, err) == ("origin", "refs/heads/feature", None)

    def test_returns_url_remote(self) -> None:
        """A URL-valued upstream is returned verbatim."""
        with (
            mock.patch.object(
                git_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                git_mod, "git_config_get", return_value=(URL_REMOTE, None)
            ),
            mock.patch.object(
                git_mod,
                "git_config_get_first",
                return_value=("refs/heads/feature", None),
            ),
        ):
            remote, branch, err = git_mod.resolve_upstream(REPO)
        assert (remote, branch, err) == (URL_REMOTE, "refs/heads/feature", None)

    def test_returns_none_without_configured_upstream(self) -> None:
        """An unset branch.remote and branch.merge yield no upstream."""
        with (
            mock.patch.object(
                git_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(git_mod, "git_config_get", return_value=(None, None)),
            mock.patch.object(
                git_mod, "git_config_get_first", return_value=(None, None)
            ),
        ):
            remote, branch, err = git_mod.resolve_upstream(REPO)
        assert (remote, branch, err) == (None, None, None)

    def test_returns_local_dot_remote(self) -> None:
        """A local upstream (``remote = .``) keeps its branch for comparison."""
        with (
            mock.patch.object(
                git_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(git_mod, "git_config_get", return_value=(".", None)),
            mock.patch.object(
                git_mod,
                "git_config_get_first",
                return_value=("refs/heads/main", None),
            ),
        ):
            remote, branch, err = git_mod.resolve_upstream(REPO)
        assert (remote, branch, err) == (".", "refs/heads/main", None)

    def test_returns_none_for_detached_head(self) -> None:
        """A detached HEAD has no branch, so it has no upstream."""
        with mock.patch.object(git_mod, "current_branch", return_value=(None, None)):
            remote, branch, err = git_mod.resolve_upstream(REPO)
        assert (remote, branch, err) == (None, None, None)

    def test_propagates_branch_lookup_failure(self) -> None:
        """A failed branch lookup fails open with its error, not a silent skip."""
        failure = "git branch --show-current failed: fatal: not a git repository"
        with mock.patch.object(git_mod, "current_branch", return_value=(None, failure)):
            remote, branch, err = git_mod.resolve_upstream(REPO)
        assert (remote, branch) == (None, None)
        assert err == failure

    def test_reports_remote_config_error(self) -> None:
        """A branch.remote read failure is surfaced."""
        with (
            mock.patch.object(
                git_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                git_mod,
                "git_config_get",
                return_value=(None, "fatal: bad config"),
            ),
        ):
            remote, branch, err = git_mod.resolve_upstream(REPO)
        assert (remote, branch) == (None, None)
        assert err == "fatal: bad config"

    def test_reports_merge_config_error(self) -> None:
        """A branch.merge read failure is surfaced."""
        with (
            mock.patch.object(
                git_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(git_mod, "git_config_get", return_value=("origin", None)),
            mock.patch.object(
                git_mod,
                "git_config_get_first",
                return_value=(None, "fatal: bad config"),
            ),
        ):
            remote, branch, err = git_mod.resolve_upstream(REPO)
        assert (remote, branch) == (None, None)
        assert err == "fatal: bad config"


# ---------------------------------------------------------------------------
# has_unpushed_commits
# ---------------------------------------------------------------------------
