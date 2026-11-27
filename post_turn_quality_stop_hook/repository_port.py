"""Domain-facing repository operations the hook's gates and preparation ask.

Preparation needs to know which paths changed, the uncommitted-changes gate
needs to know whether any work is outstanding, and both need the checked-out
branch. Those are questions about the repository, not about Git's command-line
interface, so this module states them in domain terms. The Git adapter in
:mod:`.repository_git` answers them; the pipeline and the gate module import
only this port, which keeps the decision path independent of how the answer is
obtained.

The port deliberately offers no path- or exclusion-shaped parameters. A caller
asks *which paths changed since this commit*, and the adapter decides how that
question is answered against a real worktree. That keeps knowledge of storage
locations, ignore rules, and individual Git invocations out of gate policy.

Every method binds its repository root at construction and returns the Git
adapter's own ``(value, HookError | None)`` shape, so a failed question is
reported rather than raised and a caller cannot mistake an error for a negative
answer.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.errors import HookError


class RepositoryOperations(typ.Protocol):
    """The repository questions the hook's decision path asks.

    Each answer is a value, or ``None`` with a
    :class:`~.errors.HookError` when the question could not be answered. The
    distinction matters for
    :meth:`has_uncommitted_work`: ``False`` means the question was answered and
    the tree is clean, while ``(None, error)`` means it was not answered at
    all. A caller that treats the two alike would silently report an
    unreadable tree as clean.

    The root is bound when the implementation is constructed, so no method
    takes a repository path. A test therefore supplies one prepared answer per
    question and asserts the decision without naming a worktree.
    """

    def changed_paths_since(
        self, base_commit: str
    ) -> tuple[tuple[str, ...] | None, HookError | None]:
        """Return the paths changed relative to ``base_commit``.

        The result is sorted and deduplicated, and covers tracked work
        whether staged or unstaged plus untracked files Git does not ignore.
        Ignored files are absent because they are not work the hook would ask
        the operator to commit.
        """
        ...

    def has_uncommitted_work(self) -> tuple[bool | None, HookError | None]:
        """Return whether the worktree holds uncommitted or untracked work.

        ``True`` covers staged, unstaged, and non-ignored untracked work.
        ``False`` is a definite answer that the tree is clean; ``None`` with an
        error means the question could not be answered.
        """
        ...

    def current_branch(self) -> tuple[str | None, HookError | None]:
        """Return the checked-out branch, or ``None`` for a detached ``HEAD``."""
        ...


class RepositoryOperationsFactory(typ.Protocol):
    """Build the repository operations for one resolved repository root.

    The pipeline resolves the root before it asks any repository question, so
    the factory receives a path that Git itself reported. Binding the root here
    is what lets the operations methods stay free of path parameters.
    """

    def __call__(self, repo: Path) -> RepositoryOperations:
        """Return operations bound to ``repo``."""
        ...
