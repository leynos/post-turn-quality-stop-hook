"""Git adapter for the branch-state port.

The domain gate imports only :mod:`.branch_state_port`. This module is the one
implementation that reaches :mod:`.git`, and the entry point builds it once for
the run. Every method delegates unchanged, so the port's answer shape is the
Git helper's own and no decision is made here.
"""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook import git

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.errors import HookError
    from post_turn_quality_stop_hook.fetch_port import FetchReporter


class GitBranchState:
    """Answer the branch-state port's questions through ``git.py``."""

    def current_branch(  # ruff:ignore[no-self-use]  # instance method is the port contract.
        self, repo: Path
    ) -> tuple[str | None, HookError | None]:
        """Return the checked-out branch, or ``None`` for a detached HEAD."""
        return git.current_branch(repo)

    def head_commit(  # ruff:ignore[no-self-use]  # instance method is the port contract.
        self, repo: Path
    ) -> tuple[str | None, HookError | None]:
        """Return the commit ``HEAD`` names, or an error."""
        return git.head_commit(repo)

    def ensure_remote_branch_ref(  # ruff:ignore[too-many-arguments,no-self-use]  # mirrors the Git helper it delegates to; the instance method is the port contract.
        self,
        repo: Path,
        remote: str,
        branch: str,
        *,
        always_fetch: bool,
        observer: FetchReporter | None = None,
    ) -> tuple[bool, HookError | None, bool]:
        """Ensure ``refs/remotes/<remote>/<branch>`` exists, fetching if asked.

        Parameters
        ----------
        repo
            Repository path.
        remote
            Remote name whose tracking ref should exist.
        branch
            Branch name whose tracking ref should exist.
        always_fetch
            Whether to fetch before checking the ref.
        observer
            Optional reporter notified only when a fetch actually runs.

        Returns
        -------
        tuple[bool, HookError | None, bool]
            ``ref_available`` is ``True`` when the tracking ref exists and can
            be read after the operation; it is ``False`` when the ref is still
            missing or could not be read. ``error`` explains a failed ref read
            or fetch and is ``None`` on success; a caller must treat any
            non-``None`` error as an undetermined answer and fail open rather
            than reporting a verified pass. ``fetched`` records whether a fetch
            was attempted, which is independent of ``ref_available``: a fetch
            can run and still leave the ref unreadable.

        """
        return git.ensure_remote_branch_ref(
            repo,
            remote,
            branch,
            always_fetch=always_fetch,
            observer=observer,
        )

    def merge_base(  # ruff:ignore[no-self-use]  # instance method is the port contract.
        self, repo: Path, ref: str, *, commit: str = "HEAD"
    ) -> tuple[str | None, HookError | None]:
        """Return the merge base of ``ref`` and ``commit``."""
        return git.merge_base(repo, ref, commit=commit)

    def is_ancestor(  # ruff:ignore[no-self-use]  # instance method is the port contract.
        self, repo: Path, ancestor: str, descendant: str
    ) -> tuple[bool | None, HookError | None]:
        """Return whether one commit is an ancestor of another."""
        return git.is_ancestor(repo, ancestor, descendant)

    def remote_url(  # ruff:ignore[no-self-use]  # instance method is the port contract.
        self, repo: Path, remote: str
    ) -> tuple[str | None, HookError | None]:
        """Return the configured URL of ``remote``."""
        return git.remote_url(repo, remote)
