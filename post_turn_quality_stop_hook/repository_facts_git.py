"""Read the log-only repository facts through the Git plumbing.

This is the adapter side of
:class:`post_turn_quality_stop_hook.repository_facts.RepositoryFactsSource`. It
is the only place that binds the observation's queries to ``git.py``, so the
domain module stays free of Git and subprocess concerns and the composition
root has one obvious wiring site.

Every method returns the plumbing's ``(value, HookError)`` pair unchanged. The
observation withholds a failed value and reports one bounded failure, so a Git
diagnostic is never mistaken for a negative answer and never reaches the
durable record.
"""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook import git

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.errors import HookError


class GitRepositoryFactsSource:
    """Answer the fact queries from a real repository.

    The adapter holds no state: each call is one Git query, so the observation
    always reads the repository as it is at that moment.
    """

    def current_branch(  # ruff:ignore[no-self-use]  # instance method is the port contract.
        self, repo: Path
    ) -> tuple[str | None, HookError | None]:
        """Return the checked-out branch, or ``None`` with the read failure."""
        return git.current_branch(repo)

    def head_commit(  # ruff:ignore[no-self-use]  # instance method is the port contract.
        self, repo: Path
    ) -> tuple[str | None, HookError | None]:
        """Return the current ``HEAD`` commit, or ``None`` with the failure."""
        return git.head_commit(repo)

    def has_uncommitted_changes(  # ruff:ignore[no-self-use]  # instance method is the port contract.
        self, repo: Path
    ) -> tuple[bool | None, HookError | None]:
        """Return whether the worktree is dirty, or ``None`` with the failure."""
        return git.has_uncommitted_changes(repo)

    def is_ahead_of_upstream(  # ruff:ignore[no-self-use]  # instance method is the port contract.
        self, repo: Path, upstream_ref: str
    ) -> tuple[bool | None, HookError | None]:
        """Return whether ``HEAD`` is ahead of ``upstream_ref``, or the failure."""
        return git.has_unpushed_commits(repo, upstream_ref)
