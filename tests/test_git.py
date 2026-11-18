"""Exercise git plumbing: uncommitted/upstream/snapshot operations."""

from __future__ import annotations

import dataclasses
import os
import shlex
import shutil
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from post_turn_quality_stop_hook import git as git_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import process as process_mod
from post_turn_quality_stop_hook import redaction as redaction_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook import upstream as upstream_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.git_facts import GitFacts

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


def _process_state(stat_line: str) -> str | None:
    """Return the state letter from one ``/proc/<pid>/stat`` line.

    The line reads ``pid (comm) state ...``, and ``comm`` is the process'
    self-chosen name, which may contain spaces and parentheses. Splitting the
    line on whitespace therefore cannot locate the state field; it starts
    after the final ``)``. ``None`` means the line could not be parsed.
    """
    close = stat_line.rfind(")")
    if close == -1:
        return None
    remainder = stat_line[close + 1 :].split()
    return remainder[0] if remainder else None


def _unused_upstream_source(
    _repo: Path, _facts: GitFacts | None
) -> upstream_mod.GitUpstreamHeadSource:
    """Build a port that a preparation failure must never consult."""
    return upstream_mod.GitUpstreamHeadSource(
        _repo,
        _facts,
        refresh_upstream=True,
        fetch_timeout_seconds=10.0,
    )


def _is_alive(pid: int) -> bool:
    """Return whether a process is running, without signalling it.

    ``kill(pid, 0)`` also succeeds for a process that has exited but not yet
    been reaped, so it cannot distinguish a survivor from a zombie. A killed
    descendant is reparented and may stay unreaped for a while, which would
    make the wait loop below time out even though the group kill worked. On
    Linux the process state in ``/proc`` separates the two cases.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.parent.is_dir():
        # No /proc: fall back to the existence check above.
        return True
    try:
        stat_line = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    # ``Z`` is an exited, unreaped child, which counts as gone here. An
    # unparsable line is treated as alive so the caller keeps waiting rather
    # than declaring a survivor gone.
    return _process_state(stat_line) != "Z"


REPO = Path("/fake/repo")
URL_REMOTE = "https://example.invalid/hub.git"
TIMEOUT_RC = 124

requires_process_groups = pytest.mark.skipif(
    not process_mod._SUPPORTS_PROCESS_GROUPS or not hasattr(signal, "SIGKILL"),
    reason="requires POSIX process-group signalling",
)

requires_proc_stat = pytest.mark.skipif(
    not Path("/proc/self/stat").is_file(), reason="requires Linux /proc"
)

# ---------------------------------------------------------------------------
# _process_state
# ---------------------------------------------------------------------------


class TestProcessState:
    """Tests for the ``/proc/<pid>/stat`` probe used by ``_is_alive``."""

    @pytest.mark.parametrize(
        ("stat_line", "expected"),
        [
            ("1234 (git) S 1 1234 1234 0 -1 4194560", "S"),
            ("1234 (my worker) Z 1 1234 1234 0 -1 4194560", "Z"),
            ("1234 (a (b) c) R 1 1234 1234 0 -1 4194560", "R"),
        ],
    )
    def test_reads_the_state_after_the_command_name(
        self, stat_line: str, expected: str
    ) -> None:
        """Spaces and parentheses inside ``comm`` must not shift the state."""
        assert _process_state(stat_line) == expected

    def test_returns_none_for_an_unparsable_line(self) -> None:
        """A line without a command name yields no state."""
        assert _process_state("not-a-stat-line") is None
        assert _process_state("1234 (git)") is None

    @requires_proc_stat
    def test_a_running_child_with_a_space_in_its_name_is_alive(self) -> None:
        """A live child with a spaced ``comm`` is alive; its zombie is not."""
        # ``/proc/self/comm`` is the only way to give a process a name that
        # contains spaces; writing it avoids loading libc through FFI.
        script = (
            "import pathlib, time;"
            "pathlib.Path('/proc/self/comm').write_bytes(b'my worker');"
            "time.sleep(30)"
        )
        proc = subprocess.Popen([sys.executable, "-c", script])  # noqa: S603
        try:
            stat_path = Path(f"/proc/{proc.pid}/stat")
            named = False
            ready_by = time.monotonic() + 5.0
            while time.monotonic() < ready_by:
                named = "my worker" in stat_path.read_text(encoding="utf-8")
                if named:
                    break
                time.sleep(0.02)
            assert named, "the child must have renamed itself before the probe runs"
            assert _is_alive(proc.pid), "a running child must count as alive"

            proc.kill()
            gone_by = time.monotonic() + 5.0
            while _is_alive(proc.pid) and time.monotonic() < gone_by:
                time.sleep(0.02)
            assert not _is_alive(proc.pid), (
                "an exited but unreaped child must not count as alive"
            )
        finally:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# has_uncommitted_changes

# ---------------------------------------------------------------------------


class TestHasUncommittedChanges:
    """Tests for has_uncommitted_changes()."""

    def test_clean_working_tree(self) -> None:
        """All three checks pass -> False."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0),  # git diff --quiet
                _completed(0),  # git diff --cached --quiet
                _completed(0, stdout=""),  # git ls-files
            ]
            dirty, err = git_mod.has_uncommitted_changes(REPO)
        assert dirty is False, f"expected dirty to be False but was {dirty!r}"
        assert err is None, f"expected no error but got {err!r}"
        expected_calls = 3
        assert mock_run.call_count == expected_calls
        assert mock_run.call_args_list[0] == mock.call(["git", "diff", "--quiet"], REPO)
        assert mock_run.call_args_list[1] == mock.call(
            ["git", "diff", "--cached", "--quiet"], REPO
        )
        assert mock_run.call_args_list[2] == mock.call(
            ["git", "ls-files", "--others", "--exclude-standard"], REPO
        )

    def test_unstaged_changes(self) -> None:
        """Git diff --quiet exits 1 -> True."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(1)
            dirty, err = git_mod.has_uncommitted_changes(REPO)
        assert dirty is True, f"expected dirty to be True but was {dirty!r}"
        assert err is None, f"expected no error but got {err!r}"
        mock_run.assert_called_once_with(["git", "diff", "--quiet"], REPO)

    def test_staged_changes(self) -> None:
        """Git diff --cached --quiet exits 1 -> True."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0),  # unstaged clean
                _completed(1),  # staged dirty
            ]
            dirty, err = git_mod.has_uncommitted_changes(REPO)
        assert dirty is True, f"expected dirty to be True but was {dirty!r}"
        assert err is None, f"expected no error but got {err!r}"
        expected_calls = 2
        assert mock_run.call_count == expected_calls
        assert mock_run.call_args_list[0] == mock.call(["git", "diff", "--quiet"], REPO)
        assert mock_run.call_args_list[1] == mock.call(
            ["git", "diff", "--cached", "--quiet"], REPO
        )

    def test_untracked_files(self) -> None:
        """ls-files returns output -> True."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0),
                _completed(0),
                _completed(0, stdout="newfile.py\n"),
            ]
            dirty, err = git_mod.has_uncommitted_changes(REPO)
        assert dirty is True, f"expected dirty to be True but was {dirty!r}"
        assert err is None, f"expected no error but got {err!r}"
        assert mock_run.call_args_list[2] == mock.call(
            ["git", "ls-files", "--others", "--exclude-standard"], REPO
        )

    def test_diff_error(self) -> None:
        """Non-0/1 exit from diff -> None + error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(128, stderr="fatal: bad")
            dirty, err = git_mod.has_uncommitted_changes(REPO)
        assert dirty is None, f"expected dirty to be None on error but was {dirty!r}"
        assert err is not None, "expected an error message from git diff failure"
        assert "fatal: bad" in err, f"expected fatal error in message but got {err!r}"
        mock_run.assert_called_once_with(["git", "diff", "--quiet"], REPO)

    def test_ls_files_error(self) -> None:
        """ls-files failure -> None + error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0),
                _completed(0),
                _completed(128, stderr="fatal: oops"),
            ]
            dirty, err = git_mod.has_uncommitted_changes(REPO)
        assert dirty is None, f"expected dirty to be None on error but was {dirty!r}"
        assert err is not None, "expected an error message from git ls-files failure"
        assert "git ls-files failed" in err, (
            f"expected ls-files failure in message but got {err!r}"
        )
        assert mock_run.call_args_list[2] == mock.call(
            ["git", "ls-files", "--others", "--exclude-standard"], REPO
        )


# ---------------------------------------------------------------------------
# get_upstream_ref

# ---------------------------------------------------------------------------


class TestGetUpstreamRef:
    """Tests for get_upstream_ref()."""

    def test_returns_upstream(self) -> None:
        """Check the upstream ref is read from rev-parse output."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="origin/main\n")
            ref, err = git_mod.get_upstream_ref(REPO)
        assert ref == "origin/main", (
            f"expected upstream ref origin/main but got {ref!r}"
        )
        assert err is None, f"expected no error but got {err!r}"
        mock_run.assert_called_once_with(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            REPO,
        )

    def test_no_upstream(self) -> None:
        """Check a failed rev-parse yields no upstream ref and an error message."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(128, stderr="no upstream")
            ref, err = git_mod.get_upstream_ref(REPO)
        assert ref is None, f"expected no upstream ref but got {ref!r}"
        assert "no upstream" in (err or ""), (
            f"expected no-upstream message but got {err!r}"
        )
        mock_run.assert_called_once_with(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            REPO,
        )

    def test_empty_stdout(self) -> None:
        """Check empty rev-parse output yields no upstream ref and an error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="")
            ref, err = git_mod.get_upstream_ref(REPO)
        assert ref is None, f"expected no upstream ref but got {ref!r}"
        assert err is not None, "expected an error when upstream stdout is empty"
        mock_run.assert_called_once_with(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            REPO,
        )


# ---------------------------------------------------------------------------
# git_config_get

# ---------------------------------------------------------------------------


class TestRedactCredentials:
    """Tests for redact_credentials()."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "https://user:secret@example.invalid/repo.git",
                "https://example.invalid/repo.git",
            ),
            (
                "https://token@example.invalid/repo.git",
                "https://example.invalid/repo.git",
            ),
            ("ssh://git@example.invalid/repo.git", "ssh://example.invalid/repo.git"),
            (
                # Git splits userinfo at the last ``@``, so a raw ``@`` can
                # survive inside a password.
                "https://user:p@ss@example.invalid/repo.git",
                "https://example.invalid/repo.git",
            ),
            (
                # An ``@`` in a query belongs to the repository, not to
                # userinfo, so the hostname must survive.
                "https://example.invalid/repo.git?email=ops@example.org",
                "https://example.invalid/repo.git?email=ops@example.org",
            ),
            (
                "https://example.invalid/repo.git#ops@example.org",
                "https://example.invalid/repo.git#ops@example.org",
            ),
            (
                # Userinfo is still removed when a query follows it.
                "https://token@example.invalid/repo.git?ref=main",
                "https://example.invalid/repo.git?ref=main",
            ),
            (
                # Git can repeat the URL inside a longer diagnostic.
                "fatal: unable to access 'https://token@example.invalid/r.git/'",
                "fatal: unable to access 'https://example.invalid/r.git/'",
            ),
        ],
    )
    def test_removes_url_userinfo(self, text: str, expected: str) -> None:
        """Only URL-style userinfo is stripped."""
        assert redaction_mod.redact_credentials(text) == expected


class TestRedactRemoteSafely:
    """Tests for redact_remote_safely()."""

    @pytest.mark.parametrize(
        ("remote", "expected"),
        [
            # A remote name, a local path, and an scp-style remote cannot
            # carry URL userinfo, so they are shown unchanged.
            ("origin", "origin"),
            ("/srv/git/hub.git", "/srv/git/hub.git"),
            ("git@github.com:owner/repo.git", "git@github.com:owner/repo.git"),
            (
                "https://token@example.invalid/repo.git",
                "https://example.invalid/repo.git",
            ),
            (
                # A query or fragment with no ``@`` survives redaction.
                "https://token@example.invalid/repo.git?ref=main",
                "https://example.invalid/repo.git?ref=main",
            ),
        ],
    )
    def test_returns_a_display_safe_remote(self, remote: str, expected: str) -> None:
        """A remote with no residual credential is rendered for display."""
        assert redaction_mod.redact_remote_safely(remote) == expected

    @pytest.mark.parametrize(
        "remote",
        [
            # An unescaped ``/`` in the password stops the userinfo match
            # before the ``@``, so part of the secret lands in what looks
            # like the path. Nothing from this remote may be shown.
            "https://user:pa/ss@example.invalid/repo.git",
            "https://user:pa/ss@example.invalid:8443/repo.git",
            # The guard is deliberately conservative: an ``@`` after a valid
            # URL's query is not a credential, but it is indistinguishable
            # from one that a malformed URL left behind, so the whole remote
            # is withheld rather than risk echoing a secret.
            "https://example.invalid/repo.git?email=ops@example.org",
        ],
    )
    def test_withholds_a_remote_with_a_residual_at_sign(self, remote: str) -> None:
        """A URL that cannot be redacted completely is not rendered at all."""
        assert redaction_mod.redact_remote_safely(remote) is None


class TestRedactTextSafely:
    """Tests for redact_text_safely()."""

    def test_redacts_a_well_formed_url_in_diagnostics(self) -> None:
        """A diagnostic that repeats a valid URL keeps its host and path."""
        text = "fatal: unable to access 'https://token@example.invalid/r.git/'"
        assert redaction_mod.redact_text_safely(text) == (
            "fatal: unable to access 'https://example.invalid/r.git/'"
        )

    def test_keeps_text_without_a_url(self) -> None:
        """Ordinary diagnostics pass through unchanged."""
        assert redaction_mod.redact_text_safely("fatal: repository not found") == (
            "fatal: repository not found"
        )

    def test_withholds_text_with_a_residual_at_sign(self) -> None:
        """A malformed URL leaves a residual ``@``, so nothing is returned."""
        text = "fatal: unable to access 'https://user:pa/ss@example.invalid/r.git/'"
        assert redaction_mod.redact_text_safely(text) is None


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


class TestCurrentBranch:
    """Tests for current_branch()."""

    def test_returns_branch_name(self) -> None:
        """The current branch name is returned with no error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="feature\n")
            branch, err = git_mod.current_branch(REPO)
        assert (branch, err) == ("feature", None)

    def test_detached_head_is_not_an_error(self) -> None:
        """A detached HEAD exits 0 with no name, which is not a failure."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="\n")
            branch, err = git_mod.current_branch(REPO)
        assert (branch, err) == (None, None)

    def test_command_failure_returns_error(self) -> None:
        """A failing branch lookup is surfaced, not swallowed."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(128, stderr="fatal: not a repo")
            branch, err = git_mod.current_branch(REPO)
        assert branch is None
        assert "fatal: not a repo" in (err or "")


# ---------------------------------------------------------------------------
# resolve_upstream

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
# fetch_upstream_head

# ---------------------------------------------------------------------------


class TestFetchUpstreamHead:
    """Tests for fetch_upstream_head()."""

    def test_returns_fetched_head(self) -> None:
        """The fetched commit is read from the temporary ref and then removed."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        with (
            mock.patch.object(
                upstream_mod.uuid, "uuid4", return_value=mock.Mock(hex="deadbeef")
            ),
            mock.patch.object(upstream_mod, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(0),
                _completed(0, stdout="abc123\n"),
                _completed(0),
            ]
            head, err = upstream_mod.fetch_upstream_head(
                REPO,
                "origin",
                "feature",
                options=upstream_mod.FetchOptions(timeout=5.0),
            )
        assert head == "abc123", f"expected abc123 but got {head!r}"
        assert err is None, f"expected no error but got {err!r}"
        assert mock_run.call_args_list == [
            mock.call(
                [
                    "git",
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--no-recurse-submodules",
                    "--refmap=",
                    "--",
                    "origin",
                    f"+refs/heads/feature:{temp_ref}",
                ],
                REPO,
                timeout=5.0,
            ),
            mock.call(["git", "rev-parse", "--verify", f"{temp_ref}^{{commit}}"], REPO),
            mock.call(
                ["git", "update-ref", "-d", temp_ref],
                REPO,
                timeout=upstream_mod._REF_CLEANUP_TIMEOUT_SECONDS,
            ),
        ]

    def test_uses_injected_runner_and_temp_ref_name(self) -> None:
        """The fetch and cleanup use the injected runner and ref name."""
        seen: list[tuple[list[str], float | None]] = []
        temp_ref = "refs/post-turn-quality/fetch/injected"

        def runner(
            cmd: list[str], _repo: Path, *, timeout: float | None = None
        ) -> subprocess.CompletedProcess[str]:
            """Record each command and answer fetch and rev-parse."""
            seen.append((cmd, timeout))
            if cmd[1:2] == ["fetch"]:
                return _completed(0)
            if cmd[1:2] == ["rev-parse"]:
                return _completed(0, stdout="abc123\n")
            return _completed(0)

        head, err = upstream_mod.fetch_upstream_head(
            REPO,
            "origin",
            "feature",
            options=upstream_mod.FetchOptions(
                runner=runner,
                temp_ref_factory=lambda: temp_ref,
            ),
        )

        assert (head, err) == ("abc123", None)
        assert seen[0][0] == [
            "git",
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            "--no-recurse-submodules",
            "--refmap=",
            "--",
            "origin",
            f"+refs/heads/feature:{temp_ref}",
        ]
        assert seen[-1] == (
            ["git", "update-ref", "-d", temp_ref],
            upstream_mod._REF_CLEANUP_TIMEOUT_SECONDS,
        )

    def test_preserves_a_non_branch_upstream_ref(self) -> None:
        """A configured ref outside refs/heads is fetched unchanged."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        with (
            mock.patch.object(
                upstream_mod.uuid, "uuid4", return_value=mock.Mock(hex="deadbeef")
            ),
            mock.patch.object(upstream_mod, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(0),
                _completed(0, stdout="abc123\n"),
                _completed(0),
            ]
            head, err = upstream_mod.fetch_upstream_head(
                REPO, "origin", "refs/merge-requests/1/head"
            )
        assert (head, err) == ("abc123", None)
        assert mock_run.call_args_list[0] == mock.call(
            [
                "git",
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "--no-recurse-submodules",
                "--refmap=",
                "--",
                "origin",
                f"+refs/merge-requests/1/head:{temp_ref}",
            ],
            REPO,
            timeout=None,
        )

    def test_fetch_failure_returns_error_and_cleans_up(self) -> None:
        """A failed fetch reports its error and deletes the temporary ref."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        with (
            mock.patch.object(
                upstream_mod.uuid, "uuid4", return_value=mock.Mock(hex="deadbeef")
            ),
            mock.patch.object(upstream_mod, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(128, stderr="fatal: unreachable"),
                _completed(0),
            ]
            head, err = upstream_mod.fetch_upstream_head(REPO, "origin", "feature")
        assert head is None, f"expected no head but got {head!r}"
        assert "git fetch origin feature failed" in (err or ""), (
            f"expected fetch failure message but got {err!r}"
        )
        assert "fatal: unreachable" in (err or ""), (
            f"expected git error in message but got {err!r}"
        )
        assert mock_run.call_args_list[-1] == mock.call(
            ["git", "update-ref", "-d", temp_ref],
            REPO,
            timeout=upstream_mod._REF_CLEANUP_TIMEOUT_SECONDS,
        )

    @pytest.mark.parametrize(
        ("diagnostic", "expected"),
        [
            (
                "fatal: could not read from https://user:secret@example.invalid/repo.git",
                "fatal: could not read from https://example.invalid/repo.git",
            ),
            (
                # Git can shorten the URL to the authority only, which does
                # not match the configured remote verbatim.
                "fatal: authentication failed for https://token@example.invalid",
                "fatal: authentication failed for https://example.invalid",
            ),
            (
                # A raw ``@`` inside the password must not survive redaction.
                "fatal: could not read from https://user:p@ss@example.invalid/repo.git",
                "fatal: could not read from https://example.invalid/repo.git",
            ),
        ],
        ids=["verbatim-echo", "shortened-url", "raw-at-in-password"],
    )
    def test_fetch_failure_redacts_url_credentials(
        self, diagnostic: str, expected: str
    ) -> None:
        """URL userinfo is removed wherever Git repeats it in a diagnostic."""
        remote = "https://user:secret@example.invalid/repo.git"
        with (
            mock.patch.object(
                upstream_mod.uuid, "uuid4", return_value=mock.Mock(hex="deadbeef")
            ),
            mock.patch.object(upstream_mod, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(128, stderr=diagnostic),
                _completed(0),
            ]
            head, err = upstream_mod.fetch_upstream_head(REPO, remote, "feature")
        assert head is None, f"expected no head but got {head!r}"
        assert "secret" not in (err or ""), f"credentials leaked in {err!r}"
        assert "token@" not in (err or ""), f"credentials leaked in {err!r}"
        assert expected in (err or ""), f"expected {expected!r} in {err!r}"

    def test_rev_parse_failure_returns_error_and_cleans_up(self) -> None:
        """A failed rev-parse reports its error and deletes the temporary ref."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        with (
            mock.patch.object(
                upstream_mod.uuid, "uuid4", return_value=mock.Mock(hex="deadbeef")
            ),
            mock.patch.object(upstream_mod, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(0),
                _completed(128, stderr="fatal: bad revision"),
                _completed(0),
            ]
            head, err = upstream_mod.fetch_upstream_head(REPO, "origin", "feature")
        assert head is None, f"expected no head but got {head!r}"
        assert "git rev-parse feature failed" in (err or ""), (
            f"expected rev-parse failure message but got {err!r}"
        )
        assert mock_run.call_args_list[-1] == mock.call(
            ["git", "update-ref", "-d", temp_ref],
            REPO,
            timeout=upstream_mod._REF_CLEANUP_TIMEOUT_SECONDS,
        )

    def test_empty_rev_parse_output_returns_error(self) -> None:
        """Empty rev-parse output is an error rather than a commit."""
        with (
            mock.patch.object(
                upstream_mod.uuid, "uuid4", return_value=mock.Mock(hex="deadbeef")
            ),
            mock.patch.object(upstream_mod, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(0),
                _completed(0, stdout="\n"),
                _completed(0),
            ]
            head, err = upstream_mod.fetch_upstream_head(REPO, "origin", "feature")
        assert head is None, f"expected no head but got {head!r}"
        assert "empty output" in (err or ""), (
            f"expected empty output error but got {err!r}"
        )

    def test_delete_temp_ref_failure_is_logged_with_the_ref_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A rejected deletion is reported for cleanup and keeps the result."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"

        def runner(
            cmd: list[str], _repo: Path, *, timeout: float | None = None
        ) -> subprocess.CompletedProcess[str]:
            """Answer the fetch and rev-parse, and reject the deletion."""
            del timeout
            if cmd[1:2] == ["fetch"]:
                return _completed(0)
            if cmd[1:2] == ["rev-parse"]:
                return _completed(0, stdout="abc123\n")
            return _completed(1, stderr="fatal: reference transaction rejected")

        with caplog.at_level("WARNING", logger=upstream_mod.__name__):
            head, err = upstream_mod.fetch_upstream_head(
                REPO,
                "origin",
                "feature",
                options=upstream_mod.FetchOptions(
                    runner=runner, temp_ref_factory=lambda: temp_ref
                ),
            )

        assert (head, err) == ("abc123", None), (
            "a failed cleanup must not invalidate a successful comparison"
        )
        assert any(
            temp_ref in record.getMessage()
            and "reference transaction rejected" in record.getMessage()
            for record in caplog.records
        ), "a failed deletion must name the leftover ref and its reason"

    def test_invalid_utf8_fetch_diagnostics_become_a_returned_error(
        self, tmp_path: Path
    ) -> None:
        """A transport writing non-UTF-8 stderr returns an error, not a raise."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        script = (
            "import sys;sys.stderr.buffer.write(b'bad \\xff\\xfe bytes');sys.exit(1)"
        )

        def runner(
            cmd: list[str], _repo: Path, *, timeout: float | None = None
        ) -> subprocess.CompletedProcess[str]:
            """Run a command that fails with non-UTF-8 diagnostics."""
            if cmd[1:2] == ["fetch"]:
                return process_mod.run(
                    [sys.executable, "-c", script], tmp_path, timeout=timeout
                )
            return _completed(0)

        head, err = upstream_mod.fetch_upstream_head(
            REPO,
            "origin",
            "feature",
            options=upstream_mod.FetchOptions(
                runner=runner, temp_ref_factory=lambda: temp_ref
            ),
        )

        assert head is None, f"a failed fetch must return no head, got {head!r}"
        assert err is not None, "a failed fetch must return an error"
        assert "git fetch origin feature failed" in err
        assert "\ufffd" in err, "undecodable bytes must be replaced rather than raised"

    def test_named_remote_withholds_malformed_credential_diagnostics(self) -> None:
        """A named remote can still make Git repeat an unsafe URL in an error."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/beefcafe"
        malformed = "https://user:pa/ss@example.invalid/repo.git"

        def runner(
            cmd: list[str], _repo: Path, *, timeout: float | None = None
        ) -> subprocess.CompletedProcess[str]:
            """Fail the fetch with a diagnostic that repeats the unsafe URL."""
            if cmd[1:2] == ["fetch"]:
                return _completed(1, stderr=f"fatal: unable to access '{malformed}/'")
            return _completed(0)

        head, err = upstream_mod.fetch_upstream_head(
            REPO,
            "origin",
            "feature",
            options=upstream_mod.FetchOptions(
                runner=runner, temp_ref_factory=lambda: temp_ref
            ),
        )

        assert head is None, f"a failed fetch must return no head, got {head!r}"
        assert err is not None, "a failed fetch must return an error"
        assert "git fetch origin feature failed" in err, (
            "the error must still name the fetch that failed"
        )
        assert "pa/ss" not in err, "a credential fragment must not reach the error"
        assert malformed not in err, "the unsafe URL must not reach the error"
        assert "could not be shown safely" in err, (
            "withheld diagnostics must say so rather than look empty"
        )

    def test_delete_temp_ref_bounds_the_cleanup_command(self) -> None:
        """The ref deletion is bounded so a stalled hook cannot hang the gate."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        with mock.patch.object(
            upstream_mod, "run", return_value=_completed(124, stderr="timed out")
        ) as mock_run:
            upstream_mod._delete_temp_ref(REPO, temp_ref)
        mock_run.assert_called_once_with(
            ["git", "update-ref", "-d", temp_ref],
            REPO,
            timeout=upstream_mod._REF_CLEANUP_TIMEOUT_SECONDS,
        )


# ---------------------------------------------------------------------------
# the Git upstream adapter

# ---------------------------------------------------------------------------


class TestGitUpstreamHeadSource:
    """The Git adapter refreshes the upstream and compares it with HEAD."""

    @staticmethod
    def _facts(*, branch: str | None = "feature") -> GitFacts:
        """Return facts describing a branch tracking ``origin/feature``."""
        return GitFacts(
            primary_remote="origin",
            upstream_ref="origin/feature",
            pr_base_local_ref=None,
            local_base_commit=None,
            three_way_merge_is_configured=False,
            upstream_remote="origin",
            upstream_branch="feature",
            upstream_merge_ref="refs/heads/feature",
            branch=branch,
        )

    @staticmethod
    def _source(
        facts: GitFacts, *, refresh: bool = True
    ) -> upstream_mod.GitUpstreamHeadSource:
        """Build the adapter with the settings the hook would pass it."""
        return upstream_mod.GitUpstreamHeadSource(
            REPO,
            facts,
            refresh_upstream=refresh,
            fetch_timeout_seconds=10.0,
        )

    def test_a_bounded_fetch_with_invalid_utf8_passes_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A bounded transport failure with bad bytes fails open, not raises."""
        script = (
            "import sys;sys.stderr.buffer.write(b'bad \\xff\\xfe bytes');sys.exit(1)"
        )
        seen_timeouts: list[float | None] = []

        def runner(
            cmd: list[str], _repo: Path, *, timeout: float | None = None
        ) -> subprocess.CompletedProcess[str]:
            """Run the fetch as a real child under the requested bound."""
            if cmd[1:2] == ["fetch"]:
                seen_timeouts.append(timeout)
                return process_mod.run(
                    [sys.executable, "-c", script], tmp_path, timeout=timeout
                )
            return _completed(0, stdout="head\n")

        facts = self._facts()
        source = upstream_mod.GitUpstreamHeadSource(
            REPO, facts, refresh_upstream=True, fetch_timeout_seconds=1.5
        )
        options = state_mod.StopCheckOptions(
            always_fetch=False, max_out=12000, config=Config()
        )
        with (
            caplog.at_level("WARNING", logger=pipeline_mod.__name__),
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("feature", None)
            ),
            # The preflight reads the local branch through the pipeline's own
            # import, so patch it there as well as in the adapter.
            mock.patch.object(
                pipeline_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(upstream_mod, "run", side_effect=runner),
            mock.patch.object(
                upstream_mod, "remote_names", return_value=(["origin"], None)
            ),
        ):
            refresh = pipeline_mod.refresh_upstream_head(REPO, facts, options, source)
            decision = pipeline_mod.unpushed_commits_gate(refresh)

        assert seen_timeouts == [1.5], "the fetch must run under the configured timeout"
        assert isinstance(refresh, upstream_mod.UpstreamComparison), (
            "a failed fetch must still produce a comparison"
        )
        assert refresh.error is not None, "a failed fetch must report an error"
        assert "\ufffd" in refresh.error, (
            "undecodable bytes must be replaced and reported, not raised"
        )
        assert decision.outcome == "pass", "a failed refresh must fail open"
        assert any(
            "could not verify the upstream" in record.getMessage()
            and "\ufffd" in record.getMessage()
            for record in caplog.records
        ), "the gate must log the failed refresh with the replacement characters"

    def test_remote_name_failure_is_logged_and_decision_is_unchanged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed remote-name read warns at the boundary and still compares."""
        source = self._source(self._facts())
        with (
            caplog.at_level("WARNING", logger=upstream_mod.__name__),
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod, "run", return_value=_completed(0, stdout="head\n")
            ),
            mock.patch.object(
                upstream_mod,
                "remote_names",
                return_value=(None, "git remote failed: offline"),
            ) as read_names,
            mock.patch.object(
                upstream_mod,
                "fetch_upstream_head",
                return_value=("upstream-head", None),
            ),
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(True, None)
            ),
        ):
            comparison = source.refresh_comparison()

        read_names.assert_called_once_with(REPO)
        assert comparison == upstream_mod.UpstreamComparison(
            remote="origin", branch="feature", remote_names=None, ahead=True
        ), f"unexpected comparison {comparison!r}"
        assert any(
            "could not read remote names" in record.getMessage()
            for record in caplog.records
        ), "a failed remote-name read must be logged"

    def test_refuses_to_compare_after_the_checkout_moves(self) -> None:
        """A branch change after fact collection must not count as unpushed."""
        source = self._source(self._facts())
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("other", None)
            ),
            mock.patch.object(
                upstream_mod, "run", return_value=_completed(0, stdout="head\n")
            ),
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            comparison = source.refresh_comparison()

        fetch.assert_not_called()
        ahead.assert_not_called()
        assert isinstance(comparison, upstream_mod.UpstreamComparison)
        assert comparison.ahead is None
        assert comparison.error is not None
        assert "moved from feature to other" in comparison.error

    def test_detached_head_after_the_checkout_moves_is_reported(self) -> None:
        """A move to a detached HEAD names that state in the error."""
        source = self._source(self._facts())
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=(None, None)
            ),
            mock.patch.object(
                upstream_mod, "run", return_value=_completed(0, stdout="head\n")
            ),
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
        ):
            comparison = source.refresh_comparison()

        fetch.assert_not_called()
        assert isinstance(comparison, upstream_mod.UpstreamComparison)
        assert comparison.error is not None
        assert "moved from feature to a detached HEAD" in comparison.error

    def test_refuses_to_compare_when_head_changes_during_the_fetch(self) -> None:
        """Two different HEAD readings around the fetch invalidate the check."""
        source = self._source(self._facts())
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod,
                "run",
                side_effect=[
                    _completed(0, stdout="first\n"),
                    _completed(0, stdout="second\n"),
                ],
            ),
            mock.patch.object(
                upstream_mod,
                "fetch_upstream_head",
                return_value=("upstream-head", None),
            ),
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            comparison = source.refresh_comparison()

        ahead.assert_not_called()
        assert isinstance(comparison, upstream_mod.UpstreamComparison)
        assert comparison.ahead is None
        assert comparison.error == (
            "the checkout changed while the upstream head was fetched"
        )

    def test_local_upstream_never_fetches(self) -> None:
        """A ``.`` upstream names a local branch, which needs no transport."""
        facts = dataclasses.replace(
            self._facts(),
            upstream_remote=".",
            upstream_merge_ref="refs/heads/integration",
            upstream_branch="integration",
        )
        source = self._source(facts)
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod, "run", return_value=_completed(0, stdout="head\n")
            ),
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(False, None)
            ) as ahead,
        ):
            comparison = source.refresh_comparison()

        fetch.assert_not_called()
        ahead.assert_called_once_with(REPO, "refs/heads/integration")
        assert isinstance(comparison, upstream_mod.UpstreamComparison)
        assert comparison.ahead is False

    def test_disabled_refresh_uses_the_tracking_ref(self) -> None:
        """The opt-out reads the local tracking ref without any transport."""
        source = self._source(self._facts(), refresh=False)
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod, "run", return_value=_completed(0, stdout="head\n")
            ),
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(True, None)
            ) as ahead,
        ):
            comparison = source.refresh_comparison()

        fetch.assert_not_called()
        ahead.assert_called_once_with(REPO, "origin/feature")
        assert isinstance(comparison, upstream_mod.UpstreamComparison)
        assert comparison.ahead is True

    def test_disabled_refresh_without_a_local_ref_reports_an_error(self) -> None:
        """A URL upstream has no local ref, so the adapter fails open."""
        facts = dataclasses.replace(self._facts(), upstream_ref=None)
        source = self._source(facts, refresh=False)
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod, "run", return_value=_completed(0, stdout="head\n")
            ),
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            comparison = source.refresh_comparison()

        ahead.assert_not_called()
        assert isinstance(comparison, upstream_mod.UpstreamComparison)
        assert comparison.error == (
            "upstream refresh is disabled and the upstream is not a local ref"
        )


# ---------------------------------------------------------------------------
# has_unpushed_commits

# ---------------------------------------------------------------------------


class TestHasUnpushedCommits:
    """Tests for has_unpushed_commits()."""

    def test_ahead_of_upstream(self) -> None:
        """Check a positive commit count reports unpushed commits."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="2\n")
            ahead, err = git_mod.has_unpushed_commits(REPO, "origin/main")
        assert ahead is True, f"expected ahead to be True but was {ahead!r}"
        assert err is None, f"expected no error but got {err!r}"
        mock_run.assert_called_once_with(
            ["git", "rev-list", "--count", "origin/main..HEAD"], REPO
        )

    def test_not_ahead_of_upstream(self) -> None:
        """Check a zero commit count reports no unpushed commits."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="0\n")
            ahead, err = git_mod.has_unpushed_commits(REPO, "origin/main")
        assert ahead is False, f"expected ahead to be False but was {ahead!r}"
        assert err is None, f"expected no error but got {err!r}"
        mock_run.assert_called_once_with(
            ["git", "rev-list", "--count", "origin/main..HEAD"], REPO
        )

    def test_exactly_one_ahead(self) -> None:
        """One unpushed commit is still ahead (guards ``> 0`` against ``> 1``).

        Kills the exactly-one-ahead boundary survivor tracked in #30.
        """
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="1\n")
            ahead, err = git_mod.has_unpushed_commits(REPO, "origin/main")
        assert ahead is True, (
            f"expected ahead to be True for one commit but was {ahead!r}"
        )
        assert err is None, f"expected no error but got {err!r}"

    def test_rev_list_error(self) -> None:
        """Check a failed rev-list yields ``None`` and carries the Git error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(128, stderr="fatal: bad revision")
            ahead, err = git_mod.has_unpushed_commits(REPO, "origin/main")
        assert ahead is None, f"expected ahead to be None on error but was {ahead!r}"
        assert err is not None, "expected an error message from rev-list failure"
        assert "fatal: bad revision" in err, (
            f"expected bad revision error in message but got {err!r}"
        )
        mock_run.assert_called_once_with(
            ["git", "rev-list", "--count", "origin/main..HEAD"], REPO
        )

    def test_empty_output(self) -> None:
        """Check empty rev-list output yields ``None`` and an error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="")
            ahead, err = git_mod.has_unpushed_commits(REPO, "origin/main")
        assert ahead is None, (
            f"expected ahead to be None for empty output but was {ahead!r}"
        )
        assert "empty output" in (err or ""), (
            f"expected empty output error but got {err!r}"
        )
        mock_run.assert_called_once_with(
            ["git", "rev-list", "--count", "origin/main..HEAD"], REPO
        )

    def test_non_integer_output(self) -> None:
        """Check non-integer rev-list output yields ``None`` and an error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="two\n")
            ahead, err = git_mod.has_unpushed_commits(REPO, "origin/main")
        assert ahead is None, (
            f"expected ahead to be None for non-integer output but was {ahead!r}"
        )
        assert "non-integer" in (err or ""), (
            f"expected non-integer error but got {err!r}"
        )
        mock_run.assert_called_once_with(
            ["git", "rev-list", "--count", "origin/main..HEAD"], REPO
        )


# ---------------------------------------------------------------------------
# compush_check

# ---------------------------------------------------------------------------


class TestChangedFiles:
    """Tests for changed_files()."""

    def test_returns_sorted_union(self) -> None:
        """Three non-overlapping file lists -> sorted union."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout="z.py\nb.py\n"),  # git diff
                _completed(0, stdout="c.py\n"),  # git diff --cached
                _completed(0, stdout="a.py\n"),  # git ls-files
            ]
            files, err = git_mod.changed_files(REPO, "abc123")
        assert err is None, f"expected no error but got {err!r}"
        assert files == ["a.py", "b.py", "c.py", "z.py"], (
            f"expected sorted union but got {files!r}"
        )

    def test_deduplicates_across_sources(self) -> None:
        """Same file from two sources -> appears once."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout="x.py\n"),
                _completed(0, stdout="x.py\n"),
                _completed(0, stdout=""),
            ]
            files, err = git_mod.changed_files(REPO, "abc123")
        assert err is None, f"expected no error but got {err!r}"
        assert files == ["x.py"], f"expected deduplicated list but got {files!r}"

    def test_returns_empty_on_no_changes(self) -> None:
        """All three sources empty -> empty list."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout=""),
                _completed(0, stdout=""),
                _completed(0, stdout=""),
            ]
            files, err = git_mod.changed_files(REPO, "abc123")
        assert err is None, f"expected no error but got {err!r}"
        assert files == [], f"expected empty list but got {files!r}"

    def test_asserts_git_diff_command(self) -> None:
        """First run() call uses git diff --name-only <base>."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout=""),
                _completed(0, stdout=""),
                _completed(0, stdout=""),
            ]
            git_mod.changed_files(REPO, "abc123")
        assert mock_run.call_args_list[0] == mock.call(
            ["git", "diff", "--name-only", "abc123"], REPO
        )


# ---------------------------------------------------------------------------
# merge_base

# ---------------------------------------------------------------------------


class TestMergeBase:
    """Tests for merge_base()."""

    def test_returns_commit_hash(self) -> None:
        """Successful merge-base -> stripped commit hash."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(
                0, stdout="abc123def4567890abc123def4567890abc123\n"
            )
            commit, err = git_mod.merge_base(REPO, "origin/main")
        assert commit == "abc123def4567890abc123def4567890abc123", (
            f"expected commit hash but got {commit!r}"
        )
        assert err is None, f"expected no error but got {err!r}"

    def test_returns_none_on_failure(self) -> None:
        """Non-zero exit from merge-base -> None + error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(1, stderr="fatal: not a thing")
            commit, err = git_mod.merge_base(REPO, "origin/main")
        assert commit is None, f"expected None on failure but got {commit!r}"
        assert "fatal: not a thing" in (err or ""), (
            f"expected error message but got {err!r}"
        )

    def test_asserts_merge_base_command(self) -> None:
        """run() is called with the correct merge-base command."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="abc\n")
            git_mod.merge_base(REPO, "origin/main")
        mock_run.assert_called_once_with(
            ["git", "merge-base", "origin/main", "HEAD"], REPO
        )

    def test_empty_stdout_returns_none(self) -> None:
        """Empty stdout -> None + error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="")
            commit, err = git_mod.merge_base(REPO, "origin/main")
        assert commit is None, f"expected None for empty output but got {commit!r}"
        assert err is not None, "expected an error for empty merge-base output"


# ---------------------------------------------------------------------------
# run_build_targets

# ---------------------------------------------------------------------------


class TestRunDecoding:
    """Command output is decoded leniently so a bad byte cannot abort the hook."""

    def test_invalid_utf8_output_is_replaced_not_raised(self, tmp_path: Path) -> None:
        """A failing command may emit bytes that are not valid UTF-8."""
        script = (
            "import sys;"
            "sys.stdout.buffer.write(b'out \\xff');"
            "sys.stderr.buffer.write(b'err \\xfe');"
            "sys.exit(3)"
        )

        result = process_mod.run([sys.executable, "-c", script], tmp_path)

        assert result.returncode != 0, (
            f"expected the command to fail but got rc {result.returncode!r}"
        )
        assert "\ufffd" in result.stdout, "stdout must be decoded with replacement"
        assert "\ufffd" in result.stderr, "stderr must be decoded with replacement"


class TestRunOSError:
    """Tests for run() handling of OSError (e.g. missing cwd)."""

    def test_nonexistent_cwd_returns_error(self) -> None:
        """run() with a nonexistent cwd returns rc=1 instead of raising."""
        missing_path = Path("/nonexistent/path")
        result = process_mod.run(["git", "status"], missing_path)
        assert result.returncode == 1, (
            f"expected returncode 1 for missing cwd but got {result.returncode!r}"
        )
        assert result.args == ["git", "status"], (
            f"expected args preserved but got {result.args!r}"
        )
        assert result.stdout == "", f"expected empty stdout but got {result.stdout!r}"
        assert result.stderr, "expected stderr to describe missing cwd"
        assert str(missing_path) in result.stderr, (
            f"expected missing path in stderr but got {result.stderr!r}"
        )

    def test_run_stop_checks_nonexistent_cwd(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Full pipeline exits cleanly when start_cwd does not exist."""
        with mock.patch("shutil.which", return_value="/usr/bin/git"):
            rc = pipeline_mod.run_stop_checks(
                Path("/nonexistent/path"),
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                _unused_upstream_source,
            )
        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        assert capsys.readouterr().out == "", (
            "expected no hook output when start_cwd does not exist"
        )


# ---------------------------------------------------------------------------
# _subprocess_env
# ---------------------------------------------------------------------------


_EXTRA_DIR_PARTS = (
    (".local", "bin"),
    (".bun", "bin"),
    (".cargo", "bin"),
    (".lody", "bin"),
    ("go", "bin"),
)


class TestSubprocessEnv:
    """Tests for _subprocess_env() PATH enrichment.

    Kills the ``_subprocess_env`` PATH-enrichment survivors tracked in #30.
    """

    def test_prepends_each_tool_dir_once_in_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existing tool dirs are prepended, longest-standing last, once each."""
        for parts in _EXTRA_DIR_PARTS:
            tmp_path.joinpath(*parts).mkdir(parents=True)
        monkeypatch.setattr(
            process_mod.Path, "home", classmethod(lambda _cls: tmp_path)
        )
        monkeypatch.setenv("PATH", "/usr/bin")

        entries = process_mod._subprocess_env()["PATH"].split(os.pathsep)

        # extra_dirs are inserted at index 0 in list order, so the final
        # ordering reverses them and places the pre-existing PATH last.
        expected = [
            str(tmp_path.joinpath("go", "bin")),
            str(tmp_path.joinpath(".lody", "bin")),
            str(tmp_path.joinpath(".cargo", "bin")),
            str(tmp_path.joinpath(".bun", "bin")),
            str(tmp_path.joinpath(".local", "bin")),
            "/usr/bin",
        ]
        assert entries == expected, f"expected {expected!r} but got {entries!r}"

    def test_handles_unset_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unset PATH defaults to empty, so only tool dirs are added."""
        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True)
        monkeypatch.setattr(
            process_mod.Path, "home", classmethod(lambda _cls: tmp_path)
        )
        monkeypatch.delenv("PATH", raising=False)

        entries = process_mod._subprocess_env()["PATH"].split(os.pathsep)

        # env.get("PATH", "") must default to "", whose split yields [""].
        assert entries == [str(local_bin), ""], (
            f"expected only the tool dir and the empty tail but got {entries!r}"
        )

    def test_skips_missing_dirs_and_does_not_duplicate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only existing dirs are added, and present entries are not repeated."""
        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True)  # only this tool dir exists
        monkeypatch.setattr(
            process_mod.Path, "home", classmethod(lambda _cls: tmp_path)
        )
        monkeypatch.setenv("PATH", os.pathsep.join([str(local_bin), "/usr/bin"]))

        entries = process_mod._subprocess_env()["PATH"].split(os.pathsep)

        # The absent dirs (guard requires is_dir()) must not appear, and the
        # already-present .local/bin must not be duplicated.
        assert entries == [str(local_bin), "/usr/bin"], (
            f"expected no additions or duplicates but got {entries!r}"
        )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


class TestRun:
    """Tests for run() subprocess construction keywords.

    Kills the ``run()`` keyword-construction survivors tracked in #30.
    """

    def test_returns_text_output(self, tmp_path: Path) -> None:
        """text=True yields str (not bytes) stdout."""
        result = process_mod.run(["printf", "hello"], tmp_path)
        assert result.returncode == 0
        assert isinstance(result.stdout, str), (
            f"expected str stdout but got {type(result.stdout)!r}"
        )
        assert result.stdout == "hello"

    def test_nonzero_exit_does_not_raise(self, tmp_path: Path) -> None:
        """check=False lets a non-zero exit return rather than raise."""
        nonzero = 3
        result = process_mod.run(["sh", "-c", f"exit {nonzero}"], tmp_path)
        assert result.returncode == nonzero, (
            f"expected returncode {nonzero} without raising "
            f"but got {result.returncode!r}"
        )

    def test_enriched_path_reaches_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env=_subprocess_env() propagates the enriched PATH to the child."""
        tool_dir = tmp_path / ".local" / "bin"
        tool_dir.mkdir(parents=True)
        monkeypatch.setattr(
            process_mod.Path, "home", classmethod(lambda _cls: tmp_path)
        )
        monkeypatch.setenv("PATH", "/usr/bin")

        result = process_mod.run(["sh", "-c", "printf '%s' \"$PATH\""], tmp_path)

        assert str(tool_dir) in result.stdout.split(os.pathsep), (
            f"expected enriched PATH in child env but got {result.stdout!r}"
        )

    def test_timeout_returns_failed_process(self, tmp_path: Path) -> None:
        """A timed-out command is reported as rc=124 rather than raising."""
        result = process_mod.run(["sleep", "5"], tmp_path, timeout=0.01)
        assert result.returncode == TIMEOUT_RC, (
            f"expected returncode {TIMEOUT_RC} for timeout "
            f"but got {result.returncode!r}"
        )
        assert result.stdout == ""
        assert "timed out" in result.stderr, (
            f"expected timeout message in stderr but got {result.stderr!r}"
        )

    def test_close_streams_leaves_a_live_child_alone(self) -> None:
        """A live child's reader thread may still hold the stream lock."""
        sleep = shutil.which("sleep") or "sleep"
        proc = subprocess.Popen(  # ruff:ignore[subprocess-without-shell-equals-true]
            [sleep, "5"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            process_mod._close_streams(proc)
            assert proc.stdout is not None
            assert not proc.stdout.closed, "a live child's stdout must stay open"
            assert proc.stderr is not None
            assert not proc.stderr.closed, "a live child's stderr must stay open"
        finally:
            proc.kill()
            proc.communicate()

    def test_close_streams_closes_an_exited_child(self) -> None:
        """An exited child's pipes are closed to release descriptors."""
        shell = shutil.which("sh") or "sh"
        proc = subprocess.Popen(  # ruff:ignore[subprocess-without-shell-equals-true]
            [shell, "-c", "exit 0"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.wait()
        process_mod._close_streams(proc)
        assert proc.stdout is not None
        assert proc.stdout.closed, "an exited child's stdout must be closed"
        assert proc.stderr is not None
        assert proc.stderr.closed, "an exited child's stderr must be closed"

    @requires_process_groups
    def test_timeout_terminates_group_before_killing(self, tmp_path: Path) -> None:
        """A timed-out fetch is asked to stop before its group is killed."""
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
            ("", ""),
        ]
        with (
            mock.patch.object(process_mod.subprocess, "Popen", return_value=proc),
            mock.patch.object(process_mod.os, "getpgid", return_value=9876) as getpgid,
            mock.patch.object(process_mod.os, "killpg") as killpg,
            mock.patch.object(
                process_mod, "_process_group_alive", side_effect=[True, False]
            ),
        ):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        getpgid.assert_called_once_with(4321)
        assert killpg.call_args_list == [
            mock.call(9876, process_mod.signal.SIGTERM),
            mock.call(9876, process_mod.signal.SIGKILL),
        ]
        assert result.returncode == TIMEOUT_RC
        assert "timed out" in result.stderr

    @requires_process_groups
    def test_timeout_does_not_kill_a_process_that_stops(self, tmp_path: Path) -> None:
        """A command that exits after SIGTERM is not sent SIGKILL."""
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            ("", ""),
        ]
        with (
            mock.patch.object(process_mod.subprocess, "Popen", return_value=proc),
            mock.patch.object(process_mod.os, "getpgid", return_value=9876),
            mock.patch.object(process_mod.os, "killpg") as killpg,
            # Every group member is gone, so no escalation is needed.
            mock.patch.object(process_mod, "_process_group_alive", return_value=False),
        ):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        killpg.assert_called_once_with(9876, process_mod.signal.SIGTERM)
        assert result.returncode == TIMEOUT_RC

    @requires_process_groups
    @requires_proc_stat
    def test_timeout_kills_a_descendant_that_ignores_sigterm(
        self, tmp_path: Path
    ) -> None:
        """A surviving descendant is killed after the leader exits."""
        shell = shutil.which("sh") or "sh"
        pid_file = tmp_path / "descendant.pid"
        # The leader exits on SIGTERM, but the background subshell ignores it.
        # Ignored dispositions survive exec, so the sleep loop inherits the
        # ignore. The subshell redirects its output, so only the leader holds
        # the captured pipes once it is gone.
        # The loop is bounded so a cleanup regression cannot leave a stray
        # SIGTERM-ignoring process behind after the test fails. Sixty seconds
        # is well beyond the five-second assertion window below.
        script = (
            "trap 'exit 0' TERM; "
            "( trap '' TERM; i=0; "
            "while [ $i -lt 120 ]; do sleep 0.5; i=$((i + 1)); done ) "
            ">/dev/null 2>&1 & "
            f"echo $! > {shlex.quote(str(pid_file))}; "
            "wait"
        )

        result = process_mod.run([shell, "-c", script], tmp_path, timeout=0.5)

        assert result.returncode == TIMEOUT_RC
        descendant = int(pid_file.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 5.0
        while _is_alive(descendant) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _is_alive(descendant), (
            "the timed-out command's descendant must not survive the group kill"
        )

    @requires_process_groups
    def test_timeout_falls_back_when_group_lookup_fails(self, tmp_path: Path) -> None:
        """A vanished process group falls back to signalling the direct child."""
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
            ("", ""),
        ]
        with (
            mock.patch.object(process_mod.subprocess, "Popen", return_value=proc),
            mock.patch.object(
                process_mod.os, "getpgid", side_effect=ProcessLookupError
            ) as getpgid,
            mock.patch.object(process_mod.os, "killpg") as killpg,
        ):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        getpgid.assert_called_once_with(4321)
        killpg.assert_not_called()
        assert proc.send_signal.call_args_list == [
            mock.call(process_mod.signal.SIGTERM),
            mock.call(process_mod.signal.SIGKILL),
        ]
        assert result.returncode == TIMEOUT_RC

    @requires_process_groups
    def test_timeout_without_process_group_support_signals_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows has no process groups, so the direct child is signalled."""
        monkeypatch.setattr(process_mod, "_SUPPORTS_PROCESS_GROUPS", False)
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
            ("", ""),
        ]
        with (
            mock.patch.object(process_mod.subprocess, "Popen", return_value=proc),
            mock.patch.object(process_mod.os, "getpgid") as getpgid,
            mock.patch.object(process_mod.os, "killpg") as killpg,
        ):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        getpgid.assert_not_called()
        killpg.assert_not_called()
        assert proc.send_signal.call_args_list == [
            mock.call(process_mod.signal.SIGTERM),
            mock.call(process_mod.signal.SIGKILL),
        ]
        assert result.returncode == TIMEOUT_RC

    def test_timeout_without_sigkill_kills_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A platform without SIGKILL terminates the direct child instead."""
        monkeypatch.setattr(process_mod, "_SUPPORTS_PROCESS_GROUPS", False)
        fake_signal = mock.Mock(spec=[])
        fake_signal.SIGTERM = signal.SIGTERM
        monkeypatch.setattr(process_mod, "signal", fake_signal)
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
            ("", ""),
        ]
        with mock.patch.object(process_mod.subprocess, "Popen", return_value=proc):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.kill.assert_called_once_with()
        assert result.returncode == TIMEOUT_RC

    @requires_process_groups
    def test_timeout_leaves_a_surviving_child_s_streams_open(
        self, tmp_path: Path
    ) -> None:
        """A child that survives SIGKILL keeps its pipes until teardown.

        Closing the pipes of a live child would block on a reader thread that
        cannot finish, so cleanup must skip them and let process teardown
        reclaim the descriptors.
        """
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        # The child is still running, so ``poll()`` must report no exit.
        proc.poll.return_value = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
        ]
        stdout = mock.MagicMock()
        stderr = mock.MagicMock()
        proc.stdout = stdout
        proc.stderr = stderr
        with (
            mock.patch.object(process_mod.subprocess, "Popen", return_value=proc),
            mock.patch.object(process_mod.os, "getpgid", return_value=9876),
            mock.patch.object(process_mod.os, "killpg"),
            # The bounded group wait expires without the group emptying.
            mock.patch.object(process_mod, "_wait_for_group_exit", return_value=False),
        ):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        stdout.close.assert_not_called()
        stderr.close.assert_not_called()
        assert result.returncode == TIMEOUT_RC

    @requires_process_groups
    def test_timeout_falls_back_when_group_vanishes(self, tmp_path: Path) -> None:
        """A group that disappears before signalling uses the direct child."""
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
            ("", ""),
        ]
        with (
            mock.patch.object(process_mod.subprocess, "Popen", return_value=proc),
            mock.patch.object(process_mod.os, "getpgid", return_value=9876),
            mock.patch.object(
                process_mod.os, "killpg", side_effect=ProcessLookupError
            ) as killpg,
        ):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        # Signal 0 is the group-existence probe; only real signals are asserted.
        signalled = [c for c in killpg.call_args_list if c.args[1] != 0]
        assert signalled == [
            mock.call(9876, process_mod.signal.SIGTERM),
            mock.call(9876, process_mod.signal.SIGKILL),
        ]
        assert proc.send_signal.call_args_list == [
            mock.call(process_mod.signal.SIGTERM),
            mock.call(process_mod.signal.SIGKILL),
        ]
        assert result.returncode == TIMEOUT_RC

    def test_not_a_directory_fallback(self, tmp_path: Path) -> None:
        """A file used as cwd yields the rc=1 fallback, not an exception."""
        file_path = tmp_path / "afile"
        file_path.write_text("", encoding="utf-8")
        result = process_mod.run(["git", "status"], file_path)
        assert result.returncode == 1, (
            f"expected returncode 1 for file cwd but got {result.returncode!r}"
        )
        assert result.args == ["git", "status"]
        assert result.stdout == ""
        assert str(file_path) in result.stderr, (
            f"expected the offending path in stderr but got {result.stderr!r}"
        )
