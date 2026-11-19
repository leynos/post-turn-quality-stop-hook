"""Exercise git plumbing: uncommitted/upstream/snapshot operations."""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import typing as typ
import uuid
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from post_turn_quality_stop_hook import git as git_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook.decision_log import log_directory, prune_old_logs

if typ.TYPE_CHECKING:
    import collections.abc as cabc

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


# ---------------------------------------------------------------------------
# head_commit

# ---------------------------------------------------------------------------


class TestHeadCommit:
    """Tests for head_commit()."""

    def test_returns_head_hash(self) -> None:
        """A successful rev-parse returns the trimmed commit hash."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="abc123\n")

            commit, err = git_mod.head_commit(REPO)

        assert commit == "abc123", f"expected abc123 but got {commit!r}"
        assert err is None, f"expected no error but got {err!r}"
        mock_run.assert_called_once_with(["git", "rev-parse", "HEAD"], REPO)

    def test_failure_returns_error(self) -> None:
        """A non-zero exit is reported with git's own message."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(128, stderr="fatal: not a repo\n")

            commit, err = git_mod.head_commit(REPO)

        assert commit is None, f"expected None but got {commit!r}"
        assert err == "git rev-parse HEAD failed: fatal: not a repo"

    def test_empty_output_is_an_error(self) -> None:
        """An empty hash is treated as a failure rather than a valid HEAD."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="\n")

            commit, err = git_mod.head_commit(REPO)

        assert commit is None, f"expected None but got {commit!r}"
        assert err == "git rev-parse HEAD returned empty output"


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
            answer = git_mod.has_uncommitted_changes(REPO)
            dirty, err = answer.value, answer.error
        assert dirty is False, f"expected dirty to be False but was {dirty!r}"
        assert err is None, f"expected no error but got {err!r}"
        expected_calls = 3
        assert mock_run.call_count == expected_calls
        assert mock_run.call_args_list[0] == mock.call(["git", "diff", "--quiet"], REPO)
        assert mock_run.call_args_list[1] == mock.call(
            ["git", "diff", "--cached", "--quiet"], REPO
        )
        assert mock_run.call_args_list[2] == mock.call(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            REPO,
        )

    def test_unstaged_changes(self) -> None:
        """Git diff --quiet exits 1 -> True."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(1)
            answer = git_mod.has_uncommitted_changes(REPO)
            dirty, err = answer.value, answer.error
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
            answer = git_mod.has_uncommitted_changes(REPO)
            dirty, err = answer.value, answer.error
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
                _completed(0, stdout="newfile.py\0"),
            ]
            answer = git_mod.has_uncommitted_changes(REPO)
            dirty, err = answer.value, answer.error
        assert dirty is True, f"expected dirty to be True but was {dirty!r}"
        assert err is None, f"expected no error but got {err!r}"
        assert mock_run.call_args_list[2] == mock.call(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            REPO,
        )

    def test_diff_error(self) -> None:
        """Non-0/1 exit from diff -> None + error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(128, stderr="fatal: bad")
            answer = git_mod.has_uncommitted_changes(REPO)
            dirty, err = answer.value, answer.error
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
            answer = git_mod.has_uncommitted_changes(REPO)
            dirty, err = answer.value, answer.error
        assert dirty is None, f"expected dirty to be None on error but was {dirty!r}"
        assert err is not None, "expected an error message from git ls-files failure"
        assert "git ls-files failed" in err, (
            f"expected ls-files failure in message but got {err!r}"
        )
        assert mock_run.call_args_list[2] == mock.call(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            REPO,
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
                _completed(0, stdout="z.py\0b.py\0"),  # git diff
                _completed(0, stdout="c.py\0"),  # git diff --cached
                _completed(0, stdout="a.py\0"),  # git ls-files
            ]
            answer = git_mod.changed_files(REPO, "abc123")
            files, err = answer.value, answer.error
        assert err is None, f"expected no error but got {err!r}"
        assert files == ["a.py", "b.py", "c.py", "z.py"], (
            f"expected sorted union but got {files!r}"
        )

    def test_deduplicates_across_sources(self) -> None:
        """Same file from two sources -> appears once."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout="x.py\0"),
                _completed(0, stdout="x.py\0"),
                _completed(0, stdout=""),
            ]
            answer = git_mod.changed_files(REPO, "abc123")
            files, err = answer.value, answer.error
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
            answer = git_mod.changed_files(REPO, "abc123")
            files, err = answer.value, answer.error
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
            ["git", "diff", "--name-only", "-z", "abc123"], REPO
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


class TestRunOSError:
    """Tests for run() handling of OSError (e.g. missing cwd)."""

    def test_nonexistent_cwd_returns_error(self) -> None:
        """run() with a nonexistent cwd returns rc=1 instead of raising."""
        missing_path = Path("/nonexistent/path")
        result = git_mod.run(["git", "status"], missing_path)
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
        monkeypatch.setattr(git_mod.Path, "home", classmethod(lambda _cls: tmp_path))
        monkeypatch.setenv("PATH", "/usr/bin")

        entries = git_mod._subprocess_env()["PATH"].split(os.pathsep)

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
        monkeypatch.setattr(git_mod.Path, "home", classmethod(lambda _cls: tmp_path))
        monkeypatch.delenv("PATH", raising=False)

        entries = git_mod._subprocess_env()["PATH"].split(os.pathsep)

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
        monkeypatch.setattr(git_mod.Path, "home", classmethod(lambda _cls: tmp_path))
        monkeypatch.setenv("PATH", os.pathsep.join([str(local_bin), "/usr/bin"]))

        entries = git_mod._subprocess_env()["PATH"].split(os.pathsep)

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
        result = git_mod.run(["printf", "hello"], tmp_path)
        assert result.returncode == 0
        assert isinstance(result.stdout, str), (
            f"expected str stdout but got {type(result.stdout)!r}"
        )
        assert result.stdout == "hello"

    def test_preserves_carriage_returns(self, tmp_path: Path) -> None:
        """Decoding must not translate a carriage return into a newline."""
        result = git_mod.run(["sh", "-c", "printf 'a\\rb\\0'"], tmp_path)

        assert result.returncode == 0
        assert result.stdout == "a\rb\0", (
            f"expected the raw carriage return but got {result.stdout!r}"
        )

    def test_nonzero_exit_does_not_raise(self, tmp_path: Path) -> None:
        """check=False lets a non-zero exit return rather than raise."""
        nonzero = 3
        result = git_mod.run(["sh", "-c", f"exit {nonzero}"], tmp_path)
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
        monkeypatch.setattr(git_mod.Path, "home", classmethod(lambda _cls: tmp_path))
        monkeypatch.setenv("PATH", "/usr/bin")

        result = git_mod.run(["sh", "-c", "printf '%s' \"$PATH\""], tmp_path)

        assert str(tool_dir) in result.stdout.split(os.pathsep), (
            f"expected enriched PATH in child env but got {result.stdout!r}"
        )

    def test_not_a_directory_fallback(self, tmp_path: Path) -> None:
        """A file used as cwd yields the rc=1 fallback, not an exception."""
        file_path = tmp_path / "afile"
        file_path.write_text("", encoding="utf-8")
        result = git_mod.run(["git", "status"], file_path)
        assert result.returncode == 1, (
            f"expected returncode 1 for file cwd but got {result.returncode!r}"
        )
        assert result.args == ["git", "status"]
        assert result.stdout == ""
        assert str(file_path) in result.stderr, (
            f"expected the offending path in stderr but got {result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Property test for retention
# ---------------------------------------------------------------------------

# Surrogates and NUL cannot be represented in a filesystem path, and "/" would
# change the directory model.
_PATH_COMPONENT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00/"),
    min_size=1,
    max_size=8,
).filter(lambda value: value not in {".", ".."})

# Retention only deletes files whose first line is a record the hook writes,
# so the generated daily files must carry the real record envelope, including
# the producer marker and the common fields every record shares.
_HOOK_RECORD_LINE = (
    '{"schema_version": 1, "producer": "post-turn-quality-stop-hook",'
    ' "record_type": "start", "run_id": "run-1", "pid": 1,'
    ' "timestamp": "2026-11-06T00:00:00+00:00"}\n'
)


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    retention=st.integers(min_value=0, max_value=60),
    today=st.dates(min_value=dt.date(1970, 1, 2), max_value=dt.date(2100, 1, 1)),
    extras=st.tuples(
        st.lists(st.integers(min_value=-400, max_value=400), max_size=8),
        st.lists(_PATH_COMPONENT, max_size=4),
    ),
)
def test_retention_matches_an_independent_model(
    tmp_path: Path,
    retention: int,
    today: dt.date,
    extras: tuple[list[int], list[str]],
) -> None:
    """Generated dates and names match an independent retention model."""
    offsets, unrelated_names = extras
    # ``tmp_path`` is shared across generated inputs, so each example gets its
    # own directory and cannot observe files left by an earlier one.
    log_dir = tmp_path / f"log-{uuid.uuid4().hex}"
    log_dir.mkdir()
    dates = {today + dt.timedelta(days=offset) for offset in offsets}
    for date in dates:
        (log_dir / f"hook-{date.isoformat()}.jsonl").write_text(
            _HOOK_RECORD_LINE, encoding="utf-8"
        )
    unrelated_paths = []
    for index, name in enumerate(unrelated_names):
        path = log_dir / f"{index}-{name}.txt"
        path.write_text("", encoding="utf-8")
        unrelated_paths.append(path)

    cutoff = today - dt.timedelta(days=retention)
    expected_removed = {date for date in dates if date < cutoff}

    removed = prune_old_logs(log_dir, retention, today=today)

    assert removed == len(expected_removed), (
        f"expected {len(expected_removed)} removals but got {removed}"
    )
    for date in dates:
        path = log_dir / f"hook-{date.isoformat()}.jsonl"
        assert path.exists() is (date not in expected_removed), (
            f"unexpected existence for {date.isoformat()}"
        )
    for path in unrelated_paths:
        assert path.exists(), f"unrelated file {path.name!r} must be kept"


def _run_git(repo: Path, *args: str) -> None:
    """Run one Git command in ``repo`` with a fixed test identity."""
    git_program = shutil.which("git")
    if git_program is None:
        pytest.skip("git is required for repository fixtures")
    subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # fixed argv, no shell.
        [git_program, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


def _real_repository(path: Path) -> Path:
    """Create a Git repository with one committed file."""
    repo = path / "repo"
    repo.mkdir()
    env = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    git_program = shutil.which("git")
    if git_program is None:
        pytest.skip("git is required for repository fixtures")

    def git(*args: str) -> None:
        """Run one Git command in the new repository."""
        subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # fixed argv, no shell.
            [git_program, *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

    git("init", "-q", "-b", "main")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "initial")
    return repo


class TestCarriageReturnPaths:
    """A carriage return in the state path survives the Git query decode."""

    def test_an_untracked_daily_log_is_reported_raw(self, tmp_path: Path) -> None:
        """Git's ``-z`` output keeps the path matchable and reported as found."""
        repo = _real_repository(tmp_path)
        log_dir = log_directory(repo / "state\r")
        log_dir.mkdir(parents=True)
        daily = log_dir / "hook-2026-11-09.jsonl"
        daily.write_text("{}\n", encoding="utf-8")
        expected = "state\r/post-turn-quality-stop-hook/log/hook-2026-11-09.jsonl"

        answer = git_mod.changed_files(repo, "HEAD")
        dirty = git_mod.has_uncommitted_changes(repo)

        assert answer.error is None, f"expected no error but got {answer.error!r}"
        assert answer.value == [expected], (
            f"expected the raw path, got {answer.value!r}"
        )
        assert dirty.value is True, f"expected a dirty tree, got {dirty.value!r}"
        assert git_mod.would_appear_as_change(daily) is True, (
            "a non-ignored file inside the worktree is visible to the gates"
        )


class TestWouldAppearAsChange:
    """The location question that decides whether the log may be written."""

    def test_a_path_outside_every_worktree_is_not_visible(self, tmp_path: Path) -> None:
        """The default state directory lies outside every repository."""
        assert git_mod.would_appear_as_change(tmp_path / "log" / "daily.jsonl") is False

    def test_a_non_ignored_path_inside_the_worktree_is_visible(
        self, tmp_path: Path
    ) -> None:
        """A daily file under the repository would be an untracked change."""
        repo = _real_repository(tmp_path)

        assert git_mod.would_appear_as_change(repo / "state" / "daily.jsonl") is True

    def test_an_ignored_path_inside_the_worktree_is_not_visible(
        self, tmp_path: Path
    ) -> None:
        """Git-ignoring the log directory takes it out of every gate."""
        repo = _real_repository(tmp_path)
        (repo / ".gitignore").write_text("state/\n", encoding="utf-8")

        assert git_mod.would_appear_as_change(repo / "state" / "daily.jsonl") is False

    def test_an_ignored_directory_pattern_is_honoured(self, tmp_path: Path) -> None:
        """A pattern matching the directory covers the files beneath it."""
        repo = _real_repository(tmp_path)
        (repo / ".gitignore").write_text("**/log/\n", encoding="utf-8")

        path = repo / "nested" / "log" / "hook-2026-11-09.jsonl"

        assert git_mod.would_appear_as_change(path) is False

    def test_an_unknown_location_stays_unknown(self, tmp_path: Path) -> None:
        """A location Git cannot resolve is never reported as invisible."""
        with mock.patch.object(git_mod, "is_in_worktree", return_value=None):
            answer = git_mod.would_appear_as_change(tmp_path / "daily.jsonl")

        assert answer is None

    def test_an_unknown_ignore_answer_stays_unknown(self, tmp_path: Path) -> None:
        """An ignore query Git cannot answer refuses the write."""
        with (
            mock.patch.object(git_mod, "is_in_worktree", return_value=True),
            mock.patch.object(git_mod, "is_ignored", return_value=None),
        ):
            answer = git_mod.would_appear_as_change(tmp_path / "daily.jsonl")

        assert answer is None


class TestIsTracked:
    """Git ownership decides whether the hook may create or remove a file."""

    def test_a_committed_file_is_tracked(self, tmp_path: Path) -> None:
        """A file in the index is reported as tracked."""
        repo = _real_repository(tmp_path)

        assert git_mod.is_tracked(repo / "README.md") is True

    def test_an_untracked_file_is_not_tracked(self, tmp_path: Path) -> None:
        """A file beside the worktree index is not tracked."""
        repo = _real_repository(tmp_path)
        (repo / "untracked.txt").write_text("x\n", encoding="utf-8")

        assert git_mod.is_tracked(repo / "untracked.txt") is False

    def test_a_missing_tracked_file_is_tracked(self, tmp_path: Path) -> None:
        """Deleting a tracked file from the worktree does not untrack it."""
        repo = _real_repository(tmp_path)
        (repo / "README.md").unlink()

        assert git_mod.is_tracked(repo / "README.md") is True

    def test_a_tracked_file_with_a_missing_parent_is_tracked(
        self, tmp_path: Path
    ) -> None:
        """Discovery starts from the nearest existing ancestor.

        A tracked file whose directory was deleted still has an index entry.
        Reporting it untracked would let the sink recreate it and turn a clean
        worktree into a committed-file modification.
        """
        repo = _real_repository(tmp_path)
        nested = repo / "logs"
        nested.mkdir()
        tracked = nested / "hook-2026-11-06.jsonl"
        tracked.write_text("{}\n", encoding="utf-8")
        _run_git(repo, "add", "logs/hook-2026-11-06.jsonl")
        _run_git(repo, "commit", "-q", "-m", "track a daily log")
        shutil.rmtree(nested)

        assert git_mod.is_tracked(tracked) is True

    def test_a_path_through_a_symlink_is_resolved(self, tmp_path: Path) -> None:
        """A path through a symlink is judged as the file Git sees.

        ``link`` points deeper into the worktree, so ``link/../..`` resolves to
        the repository root while its lexical form names a path above it.
        Resolving both sides finds the file Git would report, so a committed
        file is recognised rather than the ambiguity being treated as unknown.
        """
        repo = _real_repository(tmp_path)
        deeper = repo / "deep" / "deeper"
        deeper.mkdir(parents=True)
        (repo / "link").symlink_to(deeper, target_is_directory=True)
        tracked = repo / "tracked.jsonl"
        tracked.write_text("{}\n", encoding="utf-8")
        _run_git(repo, "add", "tracked.jsonl")
        _run_git(repo, "commit", "-q", "-m", "track a file")

        assert git_mod.is_tracked(repo / "link" / ".." / ".." / tracked.name) is True

    def test_a_tracked_file_reached_through_an_outside_alias_is_tracked(
        self, tmp_path: Path
    ) -> None:
        """An outside symlink into the worktree does not hide a tracked file.

        None of the alias's lexical ancestors holds ``.git``, yet Git resolves
        the working directory physically and answers from inside the worktree.
        Reporting ``False`` here would let the sink append to a committed file.
        """
        repo = _real_repository(tmp_path)
        state = repo / "state"
        state.mkdir()
        tracked = state / "hook-2026-11-06.jsonl"
        tracked.write_text("{}\n", encoding="utf-8")
        _run_git(repo, "add", "state/hook-2026-11-06.jsonl")
        _run_git(repo, "commit", "-q", "-m", "track a daily log")
        alias = tmp_path / "alias"
        alias.symlink_to(state, target_is_directory=True)

        assert git_mod.is_tracked(alias / tracked.name) is True

    def test_an_untracked_file_reached_through_an_outside_alias_is_untracked(
        self, tmp_path: Path
    ) -> None:
        """The alias does not turn an ordinary untracked file into a refusal."""
        repo = _real_repository(tmp_path)
        state = repo / "state"
        state.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(state, target_is_directory=True)

        assert git_mod.is_tracked(alias / "hook-2026-11-06.jsonl") is False

    def test_a_path_outside_a_repository_is_not_tracked(self, tmp_path: Path) -> None:
        """A path with no worktree above it cannot be tracked."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "file.txt").write_text("x\n", encoding="utf-8")

        assert git_mod.is_tracked(outside / "file.txt") is False

    def test_a_git_failure_is_unknown_not_untracked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken index is not evidence that the path is safe to write.

        ``ls-files`` exits above 1 on an error, while 1 alone means the path is
        untracked. Treating the error as untracked would let the sink append to
        a tracked file.
        """
        repo = _real_repository(tmp_path)
        broken_index = tmp_path / "broken-index"
        broken_index.write_text("not a Git index\n", encoding="utf-8")
        monkeypatch.setenv("GIT_INDEX_FILE", str(broken_index))

        assert git_mod.is_tracked(repo / "README.md") is None


def _resolver_failing_for(alias: Path) -> cabc.Callable[..., Path]:
    """Return a resolver that raises for ``alias`` and defers otherwise."""
    original = Path.resolve

    def _resolve(path: Path, *, strict: bool = False) -> Path:
        """Resolve every path except the failing alias."""
        if path == alias:
            raise OSError
        return original(path, strict=strict)

    return _resolve


class TestIsInWorktree:
    """Retention asks only whether a path lies inside any worktree."""

    def test_a_missing_file_inside_a_repository_is_in_a_worktree(
        self, tmp_path: Path
    ) -> None:
        """Discovery starts from the nearest existing ancestor."""
        repo = _real_repository(tmp_path)

        assert git_mod.is_in_worktree(repo / "state" / "hook.jsonl") is True

    def test_a_path_outside_every_repository_is_not_in_a_worktree(
        self, tmp_path: Path
    ) -> None:
        """The normal XDG location is outside every worktree."""
        outside = tmp_path / "state"
        outside.mkdir()

        assert git_mod.is_in_worktree(outside / "hook.jsonl") is False

    def test_an_unanswerable_repository_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed discovery preserves the file rather than deleting it."""
        repo = _real_repository(tmp_path)
        monkeypatch.setattr(git_mod, "repo_root", lambda _start: (None, "boom"))

        assert git_mod.is_in_worktree(repo / "hook.jsonl") is None

    def test_an_unresolvable_alias_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A link that cannot be resolved has no known location.

        Reporting ``False`` would read as "outside every worktree" and let a
        write or deletion proceed. The resolved ancestors - the ones Git uses -
        were never inspected, so unknown is the only safe answer, and every
        repository query that shares this discovery must give it.
        """
        repo = _real_repository(tmp_path)
        alias = tmp_path / "alias"
        alias.symlink_to(repo)
        monkeypatch.setattr(git_mod.Path, "resolve", _resolver_failing_for(alias))

        target = alias / "hook.jsonl"

        assert git_mod.is_in_worktree(target) is None
        assert git_mod.is_tracked(target) is None
        assert git_mod.is_ignored(target) is None


class TestIsIgnored:
    """Retention asks Git whether an in-worktree log is ignored."""

    def test_an_ignored_file_inside_a_repository_is_confirmed(
        self, tmp_path: Path
    ) -> None:
        """Ignored paths never appear in the gates, so they may expire."""
        repo = _real_repository(tmp_path)
        (repo / ".gitignore").write_text("state/\n", encoding="utf-8")

        assert git_mod.is_ignored(repo / "state" / "hook.jsonl") is True

    def test_a_non_ignored_file_inside_a_repository_is_false(
        self, tmp_path: Path
    ) -> None:
        """An untracked path is repository content retention must preserve."""
        repo = _real_repository(tmp_path)

        assert git_mod.is_ignored(repo / "state" / "hook.jsonl") is False

    def test_a_path_outside_every_repository_is_not_ignored(
        self, tmp_path: Path
    ) -> None:
        """No ignore rule can apply outside a worktree."""
        outside = tmp_path / "state"
        outside.mkdir()

        assert git_mod.is_ignored(outside / "hook.jsonl") is False

    def test_an_unanswerable_repository_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed discovery preserves the file rather than deleting it."""
        repo = _real_repository(tmp_path)
        monkeypatch.setattr(git_mod, "repo_root", lambda _start: (None, "boom"))

        assert git_mod.is_ignored(repo / "hook.jsonl") is None


class TestFetchObserver:
    """A fetch observer hears only about fetches that actually run."""

    def test_existing_ref_does_not_notify_the_observer(self) -> None:
        """A resolvable tracking ref performs no fetch, so nothing is recorded."""
        observer = mock.Mock()
        with mock.patch.object(git_mod, "ref_exists", return_value=(True, None)):
            ok, err, fetched = git_mod.ensure_remote_branch_ref(
                REPO,
                "origin",
                "main",
                always_fetch=False,
                observer=observer,
            )

        assert (ok, err, fetched) == (True, None, False)
        observer.fetch_started.assert_not_called()
        observer.fetch_finished.assert_not_called()

    def test_fetch_reports_its_own_exit_code(self) -> None:
        """A failed fetch still reports the process's real exit status."""
        observer = mock.Mock()
        observer.fetch_started.return_value = "token"
        with (
            mock.patch.object(git_mod, "ref_exists", return_value=(False, None)),
            mock.patch.object(git_mod, "run", return_value=_completed(7)),
        ):
            ok, err, fetched = git_mod.ensure_remote_branch_ref(
                REPO,
                "origin",
                "main",
                always_fetch=False,
                observer=observer,
            )

        assert ok is False, "a non-zero fetch must be reported as a failure"
        assert err is not None
        assert fetched is False, "a failed fetch never fetched the ref"
        observer.fetch_finished.assert_called_once_with("token", 7)

    def test_base_ref_passes_the_observer_to_the_fetch(self) -> None:
        """A remote-tracking base ref forwards the observer to the fetcher."""
        observer = mock.Mock()
        with (
            mock.patch.object(
                git_mod,
                "ref_exists",
                side_effect=[(False, None), (True, None)],
            ),
            mock.patch.object(git_mod, "run", return_value=_completed(0)),
        ):
            ok, err, fetched = git_mod.ensure_base_ref(
                REPO,
                "origin/main",
                always_fetch=False,
                observer=observer,
            )

        assert (ok, err, fetched) == (True, None, True)
        observer.fetch_started.assert_called_once_with()
