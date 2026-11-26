"""Exercise the repository-operations port and its Git adapter.

The port is the only repository interface the pipeline and the uncommitted
gate receive. These tests pin two things the rest of the suite relies on: the
adapter asks the Git helper about the root it was constructed with, and each
answer keeps the helper's ``(value, error)`` shape. The real-repository cases
then check the documented meaning of the answers — sorted changed paths,
non-ignored untracked files, staged and unstaged work, and a clean tree that
answers ``False`` rather than ``None``.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from hook_support import git_run

from post_turn_quality_stop_hook import git as git_mod
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.repository_git import GitRepositoryOperations

REPO = Path("/fake/repo")


class TestGitRepositoryOperationsDelegation:
    """The adapter binds the root and passes answers through unchanged."""

    def test_changed_paths_asks_about_the_bound_root(self) -> None:
        """The base commit is the only argument the adapter adds."""
        operations = GitRepositoryOperations(REPO)
        with mock.patch.object(
            git_mod, "changed_files", return_value=(["b.py", "a.py"], None)
        ) as changed_files:
            paths, returned = operations.changed_paths_since("abc123")

        changed_files.assert_called_once_with(REPO, "abc123")
        assert paths == ("b.py", "a.py"), f"expected the helper's order, got {paths!r}"
        assert returned is None

    def test_changed_paths_preserves_a_failure(self) -> None:
        """An unreadable diff returns no paths and the same error object."""
        operations = GitRepositoryOperations(REPO)
        error = HookError("changed_files", "git diff failed")
        with mock.patch.object(git_mod, "changed_files", return_value=(None, error)):
            paths, returned = operations.changed_paths_since("abc123")

        assert paths is None, f"expected no paths, got {paths!r}"
        assert returned is error, "the helper's error must pass through unchanged"

    def test_uncommitted_work_asks_about_the_bound_root(self) -> None:
        """Dirtiness delegates with the bound root and no other argument."""
        operations = GitRepositoryOperations(REPO)
        with mock.patch.object(
            git_mod, "has_uncommitted_changes", return_value=(True, None)
        ) as dirty:
            answer, error = operations.has_uncommitted_work()

        dirty.assert_called_once_with(REPO)
        assert answer is True
        assert error is None

    def test_uncommitted_work_keeps_unknown_distinct_from_clean(self) -> None:
        """An unreadable tree is ``None`` with its error, never ``False``."""
        operations = GitRepositoryOperations(REPO)
        error = HookError("has_uncommitted_changes", "git status failed")
        with mock.patch.object(
            git_mod, "has_uncommitted_changes", return_value=(None, error)
        ):
            answer, returned = operations.has_uncommitted_work()

        assert answer is None, f"expected an unknown answer, got {answer!r}"
        assert returned is error

    def test_current_branch_asks_about_the_bound_root(self) -> None:
        """The branch read delegates with the bound root."""
        operations = GitRepositoryOperations(REPO)
        with mock.patch.object(
            git_mod, "current_branch", return_value=("feature", None)
        ) as branch:
            name, error = operations.current_branch()

        branch.assert_called_once_with(REPO)
        assert name == "feature"
        assert error is None

    def test_current_branch_preserves_a_detached_head(self) -> None:
        """A detached ``HEAD`` stays ``None`` with no error."""
        operations = GitRepositoryOperations(REPO)
        with mock.patch.object(git_mod, "current_branch", return_value=(None, None)):
            name, error = operations.current_branch()

        assert name is None
        assert error is None


class TestGitRepositoryOperationsAgainstAWorktree:
    """The adapter's answers describe a real feature-branch checkout.

    The ``repo`` fixture commits ``README.md`` and ``Makefile``, then commits
    ``src/foo.py``, and checks out ``feature``. Comparing with ``HEAD~1``
    therefore always has one committed path to report, which keeps each case
    focused on the work it adds.
    """

    def test_clean_tree_reports_only_the_committed_change(self, repo: Path) -> None:
        """A clean checkout has the committed path and no uncommitted work."""
        operations = GitRepositoryOperations(repo)

        paths, path_error = operations.changed_paths_since("HEAD~1")
        dirty, dirty_error = operations.has_uncommitted_work()
        branch, branch_error = operations.current_branch()

        assert path_error is None
        assert paths == ("src/foo.py",), f"expected only the commit, got {paths!r}"
        assert dirty_error is None
        assert dirty is False, "a clean tree must answer False, not None"
        assert branch_error is None
        assert branch == "feature"

    def test_untracked_work_is_reported(self, repo: Path) -> None:
        """A non-ignored untracked file is changed work and makes the tree dirty."""
        (repo / "notes.txt").write_text("draft\n", encoding="utf-8")
        operations = GitRepositoryOperations(repo)

        paths, path_error = operations.changed_paths_since("HEAD~1")
        dirty, dirty_error = operations.has_uncommitted_work()

        assert path_error is None
        assert paths == ("notes.txt", "src/foo.py"), (
            f"expected the untracked file in sorted order, got {paths!r}"
        )
        assert dirty_error is None
        assert dirty is True

    def test_staged_work_is_reported(self, repo: Path) -> None:
        """A staged addition is changed work and makes the tree dirty."""
        (repo / "staged.txt").write_text("ready\n", encoding="utf-8")
        git_run(repo, "add", "staged.txt")
        operations = GitRepositoryOperations(repo)

        paths, path_error = operations.changed_paths_since("HEAD~1")
        dirty, dirty_error = operations.has_uncommitted_work()

        assert path_error is None
        assert paths == ("src/foo.py", "staged.txt"), (
            f"expected the staged file in sorted order, got {paths!r}"
        )
        assert dirty_error is None
        assert dirty is True

    def test_modified_tracked_work_is_reported(self, repo: Path) -> None:
        """An unstaged edit to a tracked file is changed work and dirty."""
        (repo / "README.md").write_text("# changed\n", encoding="utf-8")
        operations = GitRepositoryOperations(repo)

        paths, path_error = operations.changed_paths_since("HEAD~1")
        dirty, dirty_error = operations.has_uncommitted_work()

        assert path_error is None
        assert paths == ("README.md", "src/foo.py"), (
            f"expected the edited file in sorted order, got {paths!r}"
        )
        assert dirty_error is None
        assert dirty is True

    def test_ignored_work_is_not_reported(self, repo: Path) -> None:
        """An ignored file is absent from the paths and does not dirty the tree."""
        (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        git_run(repo, "add", ".gitignore")
        git_run(repo, "commit", "-q", "-m", "ignore generated output")
        (repo / "ignored.txt").write_text("generated\n", encoding="utf-8")
        operations = GitRepositoryOperations(repo)

        # The ignore-rule commit moved the established base one commit back, so
        # ``HEAD~2`` is still the commit before ``src/foo.py``.
        paths, path_error = operations.changed_paths_since("HEAD~2")
        dirty, dirty_error = operations.has_uncommitted_work()

        assert path_error is None
        assert paths == (".gitignore", "src/foo.py"), (
            f"expected the committed ignore rule only, got {paths!r}"
        )
        assert dirty_error is None
        assert dirty is False, "an ignored file must not make the tree dirty"
