"""Exercise Git location and ignore queries for decision-log ownership.

These queries decide whether the log may write or delete a file, so they are
tested against a real repository rather than a mocked process.
"""

from __future__ import annotations

import os
import shutil
import typing as typ
from unittest import mock

import pytest
from git_support import (
    _real_repository,
    _run_git,
    _run_git_with_environment,
    resolver_failing_for,
)

from post_turn_quality_stop_hook import git_location as git_mod
from post_turn_quality_stop_hook import git_location_paths
from post_turn_quality_stop_hook import git_location_query as query_mod
from post_turn_quality_stop_hook import process as process_mod
from post_turn_quality_stop_hook.git_location_query import GitLocationAdapter

if typ.TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _without_inherited_git_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear repository-location variables the developer's shell may export.

    The queries under test bind the live ``os.environ``, so an exported
    ``GIT_DIR`` would make them answer for the developer's checkout instead of
    the fixture repository. A test that needs one of these variables sets it
    after this fixture runs.
    """
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def _query(*, environ: typ.Mapping[str, str] | None = None) -> GitLocationAdapter:
    """Return the location adapter for this process's environment.

    The queries take an explicit environment and runner, so a test that drives
    real Git supplies this process's view. Tests that substitute either build
    their own adapter instead.
    """
    return GitLocationAdapter(environ=os.environ if environ is None else environ)


def would_appear_as_change(
    path: Path, query: git_mod.GitLocationQuery | None = None
) -> bool | None:
    """Ask the real query, bound to this process's environment by default."""
    return git_mod.would_appear_as_change(path, _query() if query is None else query)


def is_tracked(
    path: Path, query: git_mod.GitLocationQuery | None = None
) -> bool | None:
    """Ask the real query, bound to this process's environment by default."""
    return git_mod.is_tracked(path, _query() if query is None else query)


def is_tracked_entry(
    path: Path, query: git_mod.GitLocationQuery | None = None
) -> bool | None:
    """Ask the real query, bound to this process's environment by default."""
    return git_mod.is_tracked_entry(path, _query() if query is None else query)


def is_in_worktree(
    path: Path, query: git_mod.GitLocationQuery | None = None
) -> bool | None:
    """Ask the real query, bound to this process's environment by default."""
    return git_mod.is_in_worktree(path, _query() if query is None else query)


def is_ignored(
    path: Path, query: git_mod.GitLocationQuery | None = None
) -> bool | None:
    """Ask the real query, bound to this process's environment by default."""
    return git_mod.is_ignored(path, _query() if query is None else query)


def _query_answering(
    answer: process_mod.LaunchedProcess, *, command: str
) -> GitLocationAdapter:
    """Return an adapter that answers ``command`` with a prepared result.

    The adapter receives its runner as a collaborator, so a test can supply one
    that reports the status it wants examined for the one query under test.
    Discovery still runs against the real repository the fixture built.
    """

    def run_git(  # ruff:ignore[too-many-arguments]  # mirrors the runner protocol.
        cmd: list[str],
        cwd: Path,
        *,
        unset: typ.Iterable[str] = (),
        environ: typ.Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> process_mod.LaunchedProcess:
        """Answer the query under test and defer every other command."""
        if command in cmd:
            return answer
        return typ.cast(
            "process_mod.LaunchedProcess",
            process_mod.run(cmd, cwd, unset=unset, environ=environ, timeout=timeout),
        )

    return GitLocationAdapter(environ=os.environ, run_git=run_git)


def _query_without_identity() -> GitLocationAdapter:
    """Return an adapter whose Git commands report a failed discovery."""

    def run_git(*_args: object, **_kwargs: object) -> process_mod.LaunchedProcess:
        """Report a repository Git could not describe."""
        return process_mod.LaunchedProcess(
            args=["git", "rev-parse"], returncode=128, stderr="fatal: boom"
        )

    return GitLocationAdapter(environ=os.environ, run_git=run_git)


class TestWouldAppearAsChange:
    """The location question that decides whether the log may be written."""

    def test_a_path_outside_every_worktree_is_not_visible(self, tmp_path: Path) -> None:
        """The default state directory lies outside every repository."""
        assert would_appear_as_change(tmp_path / "log" / "daily.jsonl") is False

    def test_a_non_ignored_path_inside_the_worktree_is_visible(
        self, tmp_path: Path
    ) -> None:
        """A daily file under the repository would be an untracked change."""
        repo = _real_repository(tmp_path)

        assert would_appear_as_change(repo / "state" / "daily.jsonl") is True

    def test_an_ignored_path_inside_the_worktree_is_not_visible(
        self, tmp_path: Path
    ) -> None:
        """Git-ignoring the log directory takes it out of every gate."""
        repo = _real_repository(tmp_path)
        (repo / ".gitignore").write_text("state/\n", encoding="utf-8")

        assert would_appear_as_change(repo / "state" / "daily.jsonl") is False

    def test_an_ignored_directory_pattern_is_honoured(self, tmp_path: Path) -> None:
        """A pattern matching the directory covers the files beneath it."""
        repo = _real_repository(tmp_path)
        (repo / ".gitignore").write_text("**/log/\n", encoding="utf-8")

        path = repo / "nested" / "log" / "hook-2026-11-09.jsonl"

        assert would_appear_as_change(path) is False

    def test_an_unknown_location_stays_unknown(self, tmp_path: Path) -> None:
        """A location Git cannot resolve is never reported as invisible."""
        with mock.patch.object(git_mod, "is_in_worktree", return_value=None):
            answer = git_mod.would_appear_as_change(tmp_path / "daily.jsonl", _query())

        assert answer is None

    def test_an_unknown_ignore_answer_stays_unknown(self, tmp_path: Path) -> None:
        """An ignore query Git cannot answer refuses the write."""
        with (
            mock.patch.object(git_mod, "is_in_worktree", return_value=True),
            mock.patch.object(git_mod, "is_ignored", return_value=None),
        ):
            answer = git_mod.would_appear_as_change(tmp_path / "daily.jsonl", _query())

        assert answer is None

    def test_a_dotdot_through_a_symlink_is_judged_physically(
        self, tmp_path: Path, directory_symlinks: None
    ) -> None:
        """``link/../state`` names ``deep/state``, not the ignored ``state``.

        The lexical relative path is ignored, but the file Git resolves is not.
        Reporting the lexical path as ignored would let the decision log write
        into a visible worktree location and change the hook's decision.
        """
        repo = _real_repository(tmp_path)
        deeper = repo / "deep" / "deeper"
        deeper.mkdir(parents=True)
        (repo / "link").symlink_to(deeper, target_is_directory=True)
        (repo / ".gitignore").write_text("/state/\n", encoding="utf-8")
        path = repo / "link" / ".." / "state" / "hook-2026-11-06.jsonl"

        assert is_ignored(path) is False
        assert would_appear_as_change(path) is True

    def test_an_ignore_rule_for_the_resolved_directory_is_honoured(
        self, tmp_path: Path, directory_symlinks: None
    ) -> None:
        """The resolved path is matched, so ``deep/`` hides the file."""
        repo = _real_repository(tmp_path)
        deeper = repo / "deep" / "deeper"
        deeper.mkdir(parents=True)
        (repo / "link").symlink_to(deeper, target_is_directory=True)
        (repo / ".gitignore").write_text("deep/\n", encoding="utf-8")
        path = repo / "link" / ".." / "state" / "hook-2026-11-06.jsonl"

        assert is_ignored(path) is True
        assert would_appear_as_change(path) is False


class TestIsTracked:
    """Git ownership decides whether the hook may create or remove a file."""

    def test_a_committed_file_is_tracked(self, tmp_path: Path) -> None:
        """A file in the index is reported as tracked."""
        repo = _real_repository(tmp_path)

        assert is_tracked(repo / "README.md") is True

    def test_an_untracked_file_is_not_tracked(self, tmp_path: Path) -> None:
        """A file beside the worktree index is not tracked."""
        repo = _real_repository(tmp_path)
        (repo / "untracked.txt").write_text("x\n", encoding="utf-8")

        assert is_tracked(repo / "untracked.txt") is False

    def test_a_missing_tracked_file_is_tracked(self, tmp_path: Path) -> None:
        """Deleting a tracked file from the worktree does not untrack it."""
        repo = _real_repository(tmp_path)
        (repo / "README.md").unlink()

        assert is_tracked(repo / "README.md") is True

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

        assert is_tracked(tracked) is True

    def test_a_path_through_a_symlink_is_resolved(
        self, tmp_path: Path, directory_symlinks: None
    ) -> None:
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

        assert is_tracked(repo / "link" / ".." / ".." / tracked.name) is True

    def test_a_tracked_file_reached_through_an_outside_alias_is_tracked(
        self, tmp_path: Path, directory_symlinks: None
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

        assert is_tracked(alias / tracked.name) is True

    def test_an_untracked_file_reached_through_an_outside_alias_is_untracked(
        self, tmp_path: Path, directory_symlinks: None
    ) -> None:
        """The alias does not turn an ordinary untracked file into a refusal."""
        repo = _real_repository(tmp_path)
        state = repo / "state"
        state.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(state, target_is_directory=True)

        assert is_tracked(alias / "hook-2026-11-06.jsonl") is False

    def test_a_path_outside_a_repository_is_not_tracked(self, tmp_path: Path) -> None:
        """A path with no worktree above it cannot be tracked."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "file.txt").write_text("x\n", encoding="utf-8")

        assert is_tracked(outside / "file.txt") is False

    def test_a_directory_named_like_a_glob_does_not_match_another_entry(
        self, tmp_path: Path
    ) -> None:
        """``state*`` names one directory instead of globbing ``state1``.

        ``git ls-files`` parses a bare pathname as a pathspec, so the absent
        path ``state*/hook.jsonl`` would match the tracked ``state1`` file and
        report it as tracked, letting the hook append to committed data.
        """
        repo = _real_repository(tmp_path)
        state = repo / "state1"
        state.mkdir()
        tracked = state / "hook-2026-11-06.jsonl"
        tracked.write_text("{}\n", encoding="utf-8")
        _run_git(repo, "add", "state1/hook-2026-11-06.jsonl")
        _run_git(repo, "commit", "-q", "-m", "track a daily log")

        assert is_tracked(repo / "state*" / tracked.name) is False
        assert is_tracked(tracked) is True

    def test_a_git_failure_is_unknown_not_untracked(self, tmp_path: Path) -> None:
        """A broken index is not evidence that the path is safe to write.

        ``ls-files`` exits above 1 on an error, while 1 alone means the path is
        untracked. Treating the error as untracked would let the sink append to
        a tracked file.

        The repository's own index is corrupted here rather than an inherited
        ``GIT_INDEX_FILE`` being pointed at a broken file: the location queries
        now clear that variable, so only an index the inspected worktree itself
        uses can still make Git fail. The inherited-variable case is covered
        separately by the foreign-index tests below.
        """
        repo = _real_repository(tmp_path)
        (repo / ".git" / "index").write_text("not a Git index\n", encoding="utf-8")

        assert is_tracked(repo / "README.md") is None

    def test_an_inherited_index_file_does_not_hide_a_tracked_file(
        self, tmp_path: Path
    ) -> None:
        """A foreign ``GIT_INDEX_FILE`` cannot turn a committed file untracked.

        An inherited value names another repository's index. Reading it would
        report a file committed in the inspected worktree as untracked, which
        would let the sink append to committed data.
        """
        repo = _real_repository(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        _run_git(other, "init", "-q", "-b", "main")
        (other / "other.txt").write_text("x\n", encoding="utf-8")
        _run_git(other, "add", "other.txt")
        _run_git(other, "commit", "-q", "-m", "other")

        with mock.patch.dict(
            os.environ,
            {"GIT_INDEX_FILE": str(other / ".git" / "index")},
        ):
            assert is_tracked(repo / "README.md") is True
            assert is_tracked_entry(repo / "README.md") is True

    def test_a_literal_pathspec_switch_does_not_hide_a_tracked_file(
        self, tmp_path: Path
    ) -> None:
        """A global literal-pathspec switch cannot hide a committed file.

        With the switch set, Git reads the query's explicit ``:(literal)``
        prefix as part of the file name and reports an index miss for a tracked
        file. The write policy reads that answer as permission to append to a
        file Git tracks.
        """
        repo = _real_repository(tmp_path)
        (repo / "untracked.txt").write_text("x\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"GIT_LITERAL_PATHSPECS": "1"}):
            assert is_tracked(repo / "README.md") is True
            assert is_tracked_entry(repo / "README.md") is True
            assert is_tracked(repo / "untracked.txt") is False

    def test_a_checkout_name_ending_in_a_space_keeps_its_identity(
        self, tmp_path: Path
    ) -> None:
        """A trailing space is part of the checkout's name, not output padding.

        Only Git's terminating newline may be removed from the reported
        top-level path. Trimming the space yields an identity that does not
        contain the destination, so the relative-path guard refuses logging
        even where Git ignores the path.
        """
        repo = _real_repository(tmp_path, name="repo ")
        (repo / ".gitignore").write_text("state/\n", encoding="utf-8")

        assert is_tracked(repo / "README.md") is True
        assert is_tracked(repo / "untracked.txt") is False
        assert would_appear_as_change(repo / "state" / "daily.jsonl") is False

    def test_a_launch_failure_is_unknown_not_untracked(self, tmp_path: Path) -> None:
        """Git that never started cannot answer for the index.

        ``process.run`` reports a synthetic status ``1`` when the process could
        not start, and ``ls-files`` also exits ``1`` for a path absent from the
        index. Reading the launch failure as an index miss would report a file
        Git never saw as untracked, so the boundary's own flag decides.
        """
        repo = _real_repository(tmp_path)
        unlaunched = process_mod.LaunchedProcess(
            args=["git", "ls-files"], returncode=1, stderr="boom", launched=False
        )
        command = "ls-files"

        query = _query_answering(unlaunched, command=command)

        assert is_tracked(repo / "README.md", query) is None

    def test_an_index_miss_after_a_real_launch_is_untracked(
        self, tmp_path: Path
    ) -> None:
        """A Git process that ran and found nothing still answers ``False``."""
        repo = _real_repository(tmp_path)
        launched = process_mod.LaunchedProcess(
            args=["git", "ls-files"], returncode=1, stderr="", launched=True
        )
        command = "ls-files"

        query = _query_answering(launched, command=command)

        assert is_tracked(repo / "README.md", query) is False


class TestIsTrackedEntry:
    """Retention asks Git about the directory entry, not a symlink's target."""

    def test_a_committed_file_entry_is_tracked(self, tmp_path: Path) -> None:
        """A regular entry in the index is reported as tracked."""
        repo = _real_repository(tmp_path)

        assert is_tracked_entry(repo / "README.md") is True

    def test_an_untracked_file_entry_is_not_tracked(self, tmp_path: Path) -> None:
        """An untracked name reports no index entry."""
        repo = _real_repository(tmp_path)
        (repo / "untracked.txt").write_text("x\n", encoding="utf-8")

        assert is_tracked_entry(repo / "untracked.txt") is False

    def test_a_tracked_symlink_is_tracked_though_its_target_is_not(
        self, tmp_path: Path, file_symlinks: None
    ) -> None:
        """The link is the index entry; the target is a different file.

        ``is_tracked`` resolves the link physically, so it reports the target.
        Retention unlinks the entry, so the entry query must report the link
        itself - otherwise a committed symlink would be deleted from the
        worktree.
        """
        repo = _real_repository(tmp_path)
        target = repo / "target.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        (repo / ".gitignore").write_text("target.jsonl\n", encoding="utf-8")
        link = repo / "hook-2020-01-01.jsonl"
        link.symlink_to(target)
        _run_git(repo, "add", "hook-2020-01-01.jsonl")
        _run_git(repo, "commit", "-q", "-m", "track a link")

        assert is_tracked(link) is False
        assert is_tracked_entry(link) is True

    def test_a_symlink_outside_every_worktree_reports_its_target(
        self, tmp_path: Path, file_symlinks: None
    ) -> None:
        """The target's worktree decides ``is_tracked``, not the link's.

        A daily-file link sits outside every worktree but points at a committed
        file inside one. The parent lookup finds no ``.git`` above the link, so
        returning there would report the committed target as untracked and let
        the sink append to it. The query resolves the target and discovers the
        worktree that actually holds the file, while the entry query keeps the
        link unresolved and reports it as the untracked entry it is.
        """
        repo = _real_repository(tmp_path)
        tracked = repo / "tracked.jsonl"
        tracked.write_text("{}\n", encoding="utf-8")
        _run_git(repo, "add", "tracked.jsonl")
        _run_git(repo, "commit", "-q", "-m", "track the target")
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        link = outside / "hook-2026-11-06.jsonl"
        link.symlink_to(tracked)

        assert is_tracked(link) is True, (
            "the query answers for the file a symbolic link points at"
        )
        assert is_tracked_entry(link) is False, (
            "the entry query keeps the final component unresolved"
        )

    def test_a_path_outside_every_worktree_is_not_tracked(self, tmp_path: Path) -> None:
        """No index can name an entry outside every worktree."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "file.txt").write_text("x\n", encoding="utf-8")

        assert is_tracked_entry(outside / "file.txt") is False

    def test_a_git_failure_is_unknown_not_untracked(self, tmp_path: Path) -> None:
        """A broken index preserves the file rather than approving removal.

        The repository's own index is corrupted, because the entry query now
        clears an inherited ``GIT_INDEX_FILE`` and so can no longer be made to
        fail by pointing that variable elsewhere.
        """
        repo = _real_repository(tmp_path)
        (repo / ".git" / "index").write_text("not a Git index\n", encoding="utf-8")

        assert is_tracked_entry(repo / "README.md") is None


class TestIsInWorktree:
    """Retention asks only whether a path lies inside any worktree."""

    def test_a_missing_file_inside_a_repository_is_in_a_worktree(
        self, tmp_path: Path
    ) -> None:
        """Discovery starts from the nearest existing ancestor."""
        repo = _real_repository(tmp_path)

        assert is_in_worktree(repo / "state" / "hook.jsonl") is True

    def test_a_path_outside_every_repository_is_not_in_a_worktree(
        self, tmp_path: Path
    ) -> None:
        """The normal XDG location is outside every worktree."""
        outside = tmp_path / "state"
        outside.mkdir()

        assert is_in_worktree(outside / "hook.jsonl") is False

    def test_an_unanswerable_repository_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed discovery preserves the file rather than deleting it."""
        repo = _real_repository(tmp_path)
        query = _query_without_identity()

        assert is_in_worktree(repo / "hook.jsonl", query) is None

    def test_an_environment_configured_worktree_is_located(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GIT_DIR`` and ``GIT_WORK_TREE`` name a worktree with no marker.

        A worktree configured only through the environment holds no ``.git``
        marker to find, so a marker walk alone concludes it lies outside every
        worktree and lets the hook treat its tracked files as safe to write.
        """
        repo = _real_repository(tmp_path)
        worktree = tmp_path / "separate-worktree"
        worktree.mkdir()
        monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(worktree))
        tracked = worktree / "tracked.txt"
        tracked.write_text("{}\n", encoding="utf-8")
        _run_git_with_environment(worktree, "add", "tracked.txt")
        _run_git_with_environment(worktree, "commit", "-q", "-m", "track a file")
        untracked = worktree / "untracked.txt"
        untracked.write_text("{}\n", encoding="utf-8")

        assert is_in_worktree(tracked) is True
        assert is_tracked(tracked) is True
        assert is_tracked(untracked) is False

    def test_a_foreign_git_dir_makes_a_marked_worktree_location_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GIT_DIR`` for another repository conflicts with the marker walk.

        The path lies under a ``.git`` marker, but Git would read the other
        repository's index and ignore rules. Neither answer describes the file,
        so every query must report the location as unknown and preserve it.
        """
        repo = _real_repository(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        _run_git(other, "init", "-q", "-b", "main")
        (other / "other.txt").write_text("x\n", encoding="utf-8")
        _run_git(other, "add", "other.txt")
        _run_git(other, "commit", "-q", "-m", "other")
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))

        target = repo / "state" / "hook.jsonl"

        assert is_in_worktree(target) is None
        assert is_tracked(repo / "README.md") is None
        assert is_tracked_entry(repo / "README.md") is None
        assert is_ignored(target) is None

    def test_a_matching_explicit_repository_is_still_located(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An environment naming the marker's own worktree still answers.

        Refusing every explicit repository would disable logging for a caller
        that exports the same checkout, so agreement is checked rather than
        presence alone.
        """
        repo = _real_repository(tmp_path)
        (repo / ".gitignore").write_text("state/\n", encoding="utf-8")
        monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(repo))

        assert is_in_worktree(repo / "state" / "hook.jsonl") is True
        assert is_tracked(repo / "README.md") is True
        assert is_ignored(repo / "state" / "hook.jsonl") is True

    def test_an_environment_configured_location_git_cannot_answer_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken environment repository is unknown, never outside every one.

        Reporting ``False`` would read as "no worktree can see this file" and
        let a write proceed inside a worktree Git merely failed to describe.
        """
        worktree = tmp_path / "separate-worktree"
        worktree.mkdir()
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing" / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(worktree))

        target = worktree / "hook.jsonl"

        assert is_in_worktree(target) is None
        assert is_tracked(target) is None
        assert is_ignored(target) is None

    def test_an_unresolvable_alias_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory_symlinks: None
    ) -> None:
        """A link that cannot be resolved has no known location.

        Reporting ``False`` would read as "outside every worktree" and let a
        write or deletion proceed. The resolved ancestors - the ones Git uses -
        were never inspected, so unknown is the only safe answer, and every
        repository query that shares this discovery must give it.
        """
        repo = _real_repository(tmp_path)
        alias = tmp_path / "alias"
        # Directory-link semantics, so this uses the capability the
        # ``directory_symlinks`` fixture probed on Windows.
        alias.symlink_to(repo, target_is_directory=True)
        monkeypatch.setattr(
            git_location_paths.Path, "resolve", resolver_failing_for(alias)
        )

        target = alias / "hook.jsonl"

        assert is_in_worktree(target) is None
        assert is_tracked(target) is None
        assert is_ignored(target) is None


class TestIsIgnored:
    """Retention asks Git whether an in-worktree log is ignored."""

    def test_an_ignored_file_inside_a_repository_is_confirmed(
        self, tmp_path: Path
    ) -> None:
        """Ignored paths never appear in the gates, so they may expire."""
        repo = _real_repository(tmp_path)
        (repo / ".gitignore").write_text("state/\n", encoding="utf-8")

        assert is_ignored(repo / "state" / "hook.jsonl") is True

    def test_a_non_ignored_file_inside_a_repository_is_false(
        self, tmp_path: Path
    ) -> None:
        """An untracked path is repository content retention must preserve."""
        repo = _real_repository(tmp_path)

        assert is_ignored(repo / "state" / "hook.jsonl") is False

    def test_a_path_outside_every_repository_is_not_ignored(
        self, tmp_path: Path
    ) -> None:
        """No ignore rule can apply outside a worktree."""
        outside = tmp_path / "state"
        outside.mkdir()

        assert is_ignored(outside / "hook.jsonl") is False

    def test_an_unanswerable_repository_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed discovery preserves the file rather than deleting it."""
        repo = _real_repository(tmp_path)
        query = _query_without_identity()

        assert is_ignored(repo / "hook.jsonl", query) is None

    def test_a_launch_failure_is_unknown_not_unignored(self, tmp_path: Path) -> None:
        """Git that never started cannot answer the ignore question.

        ``process.run`` reports a synthetic status ``1`` when the process could
        not start, and ``git check-ignore --quiet`` also exits ``1`` when no
        rule matches. Reading the launch failure as "no rule matches" would
        report a path Git never saw as unignored, so the boundary's own flag
        decides and the answer stays unknown.
        """
        repo = _real_repository(tmp_path)
        unlaunched = process_mod.LaunchedProcess(
            args=["git", "check-ignore"], returncode=1, stderr="boom", launched=False
        )
        command = "check-ignore"

        query = _query_answering(unlaunched, command=command)

        assert is_ignored(repo / "state" / "hook.jsonl", query) is None

    def test_a_launched_no_match_is_false(self, tmp_path: Path) -> None:
        """A Git process that ran and matched no rule still answers ``False``."""
        repo = _real_repository(tmp_path)
        launched = process_mod.LaunchedProcess(
            args=["git", "check-ignore"], returncode=1, stderr="", launched=True
        )
        command = "check-ignore"

        query = _query_answering(launched, command=command)

        assert is_ignored(repo / "state" / "hook.jsonl", query) is False

    def test_a_link_whose_target_is_ignored_is_not_itself_ignored(
        self, tmp_path: Path, file_symlinks: None
    ) -> None:
        """An untracked link is a worktree change even if its target is hidden.

        Retention removes the directory entry, so the entry is what must be
        ignored. Resolving the whole path would ask Git about the target and
        report the link as ignorable, letting a run with logging enabled delete
        an untracked entry the uncommitted-changes gate still reports.
        """
        repo = _real_repository(tmp_path)
        target = repo / "target.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        (repo / ".gitignore").write_text("target.jsonl\n", encoding="utf-8")
        link = repo / "hook-2020-01-01.jsonl"
        link.symlink_to(target)

        assert is_ignored(target) is True
        assert is_ignored(link) is False, (
            "the query must name the link, not the target it points at"
        )

    def test_a_link_named_by_an_ignore_rule_is_ignored(
        self, tmp_path: Path, file_symlinks: None
    ) -> None:
        """A rule that names the link itself still matches the entry."""
        repo = _real_repository(tmp_path)
        target = repo / "target.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        (repo / ".gitignore").write_text("hook-*.jsonl\n", encoding="utf-8")
        link = repo / "hook-2020-01-01.jsonl"
        link.symlink_to(target)

        assert is_ignored(link) is True

    def test_a_state_home_under_a_colon_directory_is_not_ignored(
        self, tmp_path: Path
    ) -> None:
        """A leading ``:`` in the relative path is not pathspec magic.

        A state home beneath ``<repo>/:/state`` yields a relative path starting
        ``:/``. Git reads that prefix as top-relative pathspec magic, so before
        the ``./`` guard it matched ``/state/`` at the repository root and
        reported the unrelated destination as ignored. The file is in fact not
        ignored, and reporting it as such would let the dirty gate miss an
        untracked change the log created.
        """
        repo = _real_repository(tmp_path)
        (repo / ".gitignore").write_text("/state/\n", encoding="utf-8")
        destination = repo / ":" / "state" / "hook-2026-11-06.jsonl"

        assert is_ignored(destination) is False
        assert would_appear_as_change(destination) is True

    def test_a_destination_an_ignore_rule_actually_matches_is_ignored(
        self, tmp_path: Path
    ) -> None:
        """The ``./`` guard keeps a genuine match working.

        The guard rewrites the pathname Git is given, so a rule that really
        matches the entry must still be reported; otherwise the guard would
        trade a false positive for a false negative.
        """
        repo = _real_repository(tmp_path)
        (repo / ".gitignore").write_text(":/state/\n", encoding="utf-8")
        destination = repo / ":" / "state" / "hook-2026-11-06.jsonl"

        assert is_ignored(destination) is True


class TestSubstitutedEnvironmentAndRunner:
    """The queries read one environment mapping and one runner, and write nothing.

    These tests substitute both collaborators, so the module is exercised
    without Git and without the live process environment. The read-only test
    pins that a query leaves the repository tree byte-for-byte and
    entry-for-entry unchanged, which is what lets retention trust the answer.
    """

    @staticmethod
    def _snapshot(root: Path) -> dict[str, tuple[int, bytes] | None]:
        """Return each entry's content and modification time beneath ``root``."""
        return {
            str(path.relative_to(root)): (
                (path.stat().st_mtime_ns, path.read_bytes()) if path.is_file() else None
            )
            for path in sorted(root.rglob("*"))
        }

    def test_a_substituted_runner_answers_every_query_without_writing(
        self, tmp_path: Path
    ) -> None:
        """An injected runner answers the queries and changes no file."""
        repo = _real_repository(tmp_path)
        (repo / ".gitignore").write_text("state/\n", encoding="utf-8")
        target = repo / "state" / "hook-2026-11-06.jsonl"
        before = self._snapshot(repo)
        calls: list[tuple[list[str], str]] = []

        def run_git(  # ruff:ignore[too-many-arguments]  # mirrors the runner protocol.
            cmd: list[str],
            cwd: Path,
            *,
            unset: typ.Iterable[str] = (),
            environ: typ.Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> process_mod.LaunchedProcess:
            """Record the command and answer it from the real checkout."""
            del unset, environ
            calls.append((list(cmd), str(cwd)))
            return typ.cast(
                "process_mod.LaunchedProcess",
                process_mod.run(cmd, cwd, environ=os.environ, timeout=timeout),
            )

        query = GitLocationAdapter(environ=dict(os.environ), run_git=run_git)

        assert is_tracked(repo / "README.md", query) is True
        assert is_tracked_entry(repo / "README.md", query) is True
        assert is_in_worktree(target, query) is True
        assert is_ignored(target, query) is True
        assert would_appear_as_change(target, query) is False
        assert self._snapshot(repo) == before, (
            "a location query must never create, modify, or remove a file"
        )
        assert all(cmd[0] == "git" for cmd, _cwd in calls), (
            f"every query must run Git itself, but got {calls!r}"
        )
        assert all(
            cmd[1] in {"rev-parse", "ls-files", "check-ignore"} for cmd, _ in calls
        ), f"a location query must stay read-only, but ran {calls!r}"

    def test_the_environment_mapping_is_read_instead_of_the_process(
        self, tmp_path: Path
    ) -> None:
        """The explicit snapshot, not ``os.environ``, decides the repository."""
        repo = _real_repository(tmp_path)
        worktree = tmp_path / "separate-worktree"
        worktree.mkdir()
        environ = dict(os.environ)
        environ["GIT_DIR"] = str(repo / ".git")
        environ["GIT_WORK_TREE"] = str(worktree)
        (worktree / "tracked.txt").write_text("{}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, environ):
            _run_git_with_environment(worktree, "add", "tracked.txt")
            _run_git_with_environment(worktree, "commit", "-q", "-m", "track a file")
        query = GitLocationAdapter(environ=environ, run_git=process_mod.run)

        assert is_in_worktree(worktree / "tracked.txt", query) is True
        assert is_tracked(worktree / "tracked.txt", query) is True
        assert is_tracked(worktree / "untracked.txt", query) is False

    def test_a_relative_git_dir_cannot_answer_from_a_nested_repository(
        self, tmp_path: Path
    ) -> None:
        """A committed file stays tracked when a relative ``GIT_DIR`` is inherited.

        The environment names the checkout through a relative ``GIT_DIR`` that
        resolves to it from the discovery directory. From the worktree top the
        same value names the repository one level up, so an unpinned index
        query would report the committed file as untracked and retention could
        delete it. Pinning every query to the validated absolute git directory
        keeps the answer describing the file's own repository.
        """
        outer = tmp_path / "outer"
        outer.mkdir()
        _run_git(tmp_path, "init", "-q", "-b", "main")
        (tmp_path / ".gitignore").write_text("", encoding="utf-8")
        _run_git(tmp_path, "add", ".gitignore")
        _run_git(tmp_path, "commit", "-q", "-m", "the enclosing repository")
        _run_git(outer, "init", "-q", "-b", "main")
        (outer / "sub").mkdir()
        tracked = outer / "sub" / "tracked.txt"
        tracked.write_text("{}\n", encoding="utf-8")
        _run_git(outer, "add", "sub/tracked.txt")
        _run_git(outer, "commit", "-q", "-m", "track a file")

        environ = dict(os.environ)
        environ["GIT_DIR"] = "../.git"
        environ["GIT_WORK_TREE"] = ".."
        query = GitLocationAdapter(environ=environ, run_git=process_mod.run)

        assert is_tracked(tracked, query) is True, (
            "a relative GIT_DIR must not let the enclosing repository's index"
            " answer for the nested checkout"
        )
        assert is_tracked_entry(tracked, query) is True

    def test_a_query_is_pinned_to_the_validated_git_directory(
        self, tmp_path: Path
    ) -> None:
        """Every index and ignore query names the absolute repository Git resolved.

        A relative ``GIT_DIR`` resolves against each command's working
        directory. The adapter therefore passes the identity's absolute paths
        explicitly, so the same relative value cannot select another repository
        from the worktree top.
        """
        repo = _real_repository(tmp_path)
        environ = dict(os.environ)
        environ["GIT_DIR"] = ".git"
        seen: list[dict[str, str]] = []

        def run_git(  # ruff:ignore[too-many-arguments]  # mirrors the runner protocol.
            cmd: list[str],
            cwd: Path,
            *,
            unset: typ.Iterable[str] = (),
            environ: typ.Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> process_mod.LaunchedProcess:
            """Record the environment each query ran with, then run it."""
            if environ is not None and "ls-files" in cmd:
                seen.append(dict(environ))
            return typ.cast(
                "process_mod.LaunchedProcess",
                process_mod.run(
                    cmd, cwd, unset=unset, environ=environ, timeout=timeout
                ),
            )

        query = GitLocationAdapter(environ=environ, run_git=run_git)

        assert is_tracked(repo / "README.md", query) is True
        assert seen, "the index query must have run"
        assert seen[0]["GIT_DIR"] == str((repo / ".git").resolve()), (
            "the query must be pinned to the absolute git directory"
        )
        assert seen[0]["GIT_WORK_TREE"] == str(repo.resolve()), (
            "the query must be pinned to the absolute worktree top"
        )
        assert "GIT_INDEX_FILE" not in seen[0], (
            "an inherited index file must not answer for this worktree"
        )

    def test_every_query_is_bounded_by_the_module_timeout(self, tmp_path: Path) -> None:
        """A stalled Git command cannot stall the hook before its decision."""
        repo = _real_repository(tmp_path)
        seen: list[float | None] = []

        def run_git(  # ruff:ignore[too-many-arguments]  # mirrors the runner protocol.
            cmd: list[str],
            cwd: Path,
            *,
            unset: typ.Iterable[str] = (),
            environ: typ.Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> process_mod.LaunchedProcess:
            """Record the deadline each query received, then run it."""
            seen.append(timeout)
            return typ.cast(
                "process_mod.LaunchedProcess",
                process_mod.run(
                    cmd, cwd, unset=unset, environ=environ, timeout=timeout
                ),
            )

        query = GitLocationAdapter(environ=dict(os.environ), run_git=run_git)

        assert is_tracked(repo / "README.md", query) is True
        assert seen, "the tracked-file question must run at least one Git command"
        assert set(seen) == {query_mod._GIT_QUERY_TIMEOUT_SECONDS}, (
            f"every location query must carry the module deadline, got {seen!r}"
        )

    def test_a_timed_out_query_is_an_unknown_answer(self, tmp_path: Path) -> None:
        """A command Git timed out on never reports an ownership answer.

        ``process.run`` reports a deadline as status 124, which is neither
        "tracked" nor "untracked". The query must therefore answer ``None``,
        which the write policy refuses, rather than permit a write.
        """
        repo = _real_repository(tmp_path)
        timed_out = process_mod.LaunchedProcess(
            args=["git", "ls-files"],
            returncode=124,
            stderr="command timed out after 5.0 seconds",
        )

        query = _query_answering(timed_out, command="ls-files")

        assert is_tracked(repo / "README.md", query) is None, (
            "a timed-out query must stay unknown so the write is refused"
        )

    def test_an_inherited_common_dir_cannot_answer_for_the_pinned_repository(
        self, tmp_path: Path
    ) -> None:
        """A foreign ``GIT_COMMON_DIR`` cannot supply the ignore rules.

        Git reads ``info/exclude`` from the common directory. An inherited
        value naming another repository would let that repository's ignore
        rules hide a path the pinned worktree really reports, so the query
        clears it and derives the common directory from the pinned
        ``GIT_DIR``.
        """
        repo = _real_repository(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        _run_git(other, "init", "-q", "-b", "main")
        (other / "other.txt").write_text("x\n", encoding="utf-8")
        _run_git(other, "add", "other.txt")
        _run_git(other, "commit", "-q", "-m", "other")
        exclude = other / ".git" / "info" / "exclude"
        exclude.write_text("state/\n", encoding="utf-8")
        environ = dict(os.environ)
        environ["GIT_COMMON_DIR"] = str(other / ".git")
        query = GitLocationAdapter(environ=environ, run_git=process_mod.run)

        assert is_ignored(repo / "state" / "hook.jsonl", query) is False, (
            "a foreign common directory must not supply this worktree's ignore rules"
        )
