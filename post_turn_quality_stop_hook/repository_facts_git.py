"""Read the log-only repository facts through the Git plumbing.

This is the adapter side of
:class:`post_turn_quality_stop_hook.repository_facts.RepositoryFactsSource`. It
is the only place that binds the observation's queries to ``git.py``, so the
domain module stays free of Git and subprocess concerns and the composition
root has one obvious wiring site.

Every method reduces the plumbing's ``(value, HookError)`` pair to the value
alone. A failure is reported as ``None``, which the observation treats as an
unknown fact to withhold rather than as a negative answer.
"""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook import git

if typ.TYPE_CHECKING:
    from pathlib import Path


class GitRepositoryFactsSource:
    """Answer the fact queries from a real repository.

    The adapter holds no state: each call is one Git query, so the observation
    always reads the repository as it is at that moment.
    """

    def current_branch(  # ruff:ignore[no-self-use]  # instance method is the port contract.
        self, repo: Path
    ) -> str | None:
        """Return the checked-out branch, or ``None`` when it cannot be read."""
        branch, _error = git.current_branch(repo)
        return branch

    def head_commit(  # ruff:ignore[no-self-use]  # instance method is the port contract.
        self, repo: Path
    ) -> str | None:
        """Return the current ``HEAD`` commit, or ``None`` when unreadable."""
        commit, _error = git.head_commit(repo)
        return commit

    def has_uncommitted_changes(  # ruff:ignore[no-self-use]  # instance method is the port contract.
        self, repo: Path
    ) -> bool | None:
        """Return whether the worktree is dirty, or ``None`` when unreadable."""
        dirty, _error = git.has_uncommitted_changes(repo)
        return dirty

    def is_ahead_of_upstream(  # ruff:ignore[no-self-use]  # instance method is the port contract.
        self, repo: Path, upstream_ref: str
    ) -> bool | None:
        """Return whether ``HEAD`` is ahead of ``upstream_ref``, or ``None``."""
        ahead, _error = git.has_unpushed_commits(repo, upstream_ref)
        return ahead
