"""Prepared ports for branch-gate unit tests.

The gate module imports no adapter, so its tests supply every answer through
the ``BranchState`` port, the pull-request lookup callable, and the target
reader callable. These doubles keep the answers and the questions each gate
asked, so a test can assert both the decision and that an avoided step was
never consulted.
"""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook.branch_state_port import BranchGatePorts
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.pull_request import PullRequestSummary

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.fetch_port import FetchReporter

#: The remote URL a PR lookup is expected to receive in these tests.
PR_REMOTE_URL = "https://github.com/o/r.git"


class FakeBranchState:
    """Answer the branch-state port from prepared values.

    Every answer is a plain attribute so a test states only what it cares
    about. The ``calls`` list names each question asked, which is how a test
    proves a avoided step (an ahead-of-upstream comparison, a dirtiness
    query, a fetch) was never consulted.
    """

    def __init__(  # ruff:ignore[too-many-arguments]  # each prepared answer is an independent input.
        self,
        *,
        branch: str | None = "feature",
        commit: str | None = "head",
        dirty: bool | None = False,
        remote: str | None = PR_REMOTE_URL,
        merged: str | None = "local",
        ancestor: bool | None = False,
        fetch_ok: bool = True,
        branch_error: HookError | None = None,
        commit_error: HookError | None = None,
        dirt_error: HookError | None = None,
        merge_error: HookError | None = None,
        ancestor_error: HookError | None = None,
        remote_error: HookError | None = None,
    ) -> None:
        """Store the answers and start the question log empty."""
        self.branch = branch
        self.commit = commit
        self.dirty = dirty
        self.remote = remote
        self.merged = merged
        self.ancestor = ancestor
        self.fetch_ok = fetch_ok
        self.branch_error = branch_error
        self.commit_error = commit_error
        self.dirt_error = dirt_error
        self.merge_error = merge_error
        self.ancestor_error = ancestor_error
        self.remote_error = remote_error
        self.calls: list[str] = []

    def current_branch(self, repo: Path) -> tuple[str | None, HookError | None]:
        """Return the prepared branch answer."""
        del repo
        self.calls.append("current_branch")
        return self.branch, self.branch_error

    def head_commit(self, repo: Path) -> tuple[str | None, HookError | None]:
        """Return the prepared commit answer."""
        del repo
        self.calls.append("head_commit")
        return self.commit, self.commit_error

    def ensure_remote_branch_ref(  # ruff:ignore[too-many-arguments]  # mirrors the port it stands in for.
        self,
        repo: Path,
        remote: str,
        branch: str,
        *,
        always_fetch: bool,
        observer: FetchReporter | None = None,
    ) -> tuple[bool, HookError | None, bool]:
        """Return the prepared fetch answer and record the request."""
        del repo, remote, branch, observer
        self.calls.append("ensure_remote_branch_ref")
        error = None if self.fetch_ok else HookError("fetch", "offline")
        return self.fetch_ok, error, always_fetch

    def has_uncommitted_changes(
        self, repo: Path
    ) -> tuple[bool | None, HookError | None]:
        """Return the prepared dirtiness answer."""
        del repo
        self.calls.append("has_uncommitted_changes")
        return self.dirty, self.dirt_error

    def merge_base(
        self, repo: Path, ref: str, *, commit: str = "HEAD"
    ) -> tuple[str | None, HookError | None]:
        """Return the prepared merge base and record the pinned commit."""
        del repo, ref
        self.calls.append(f"merge_base:{commit}")
        return self.merged, self.merge_error

    def is_ancestor(
        self, repo: Path, ancestor: str, descendant: str
    ) -> tuple[bool | None, HookError | None]:
        """Return the prepared ancestry answer."""
        del repo, ancestor, descendant
        self.calls.append("is_ancestor")
        return self.ancestor, self.ancestor_error

    def remote_url(
        self, repo: Path, remote: str
    ) -> tuple[str | None, HookError | None]:
        """Return the prepared remote URL."""
        del repo, remote
        self.calls.append("remote_url")
        return self.remote, self.remote_error


class FakePullRequestLookup:
    """Return a prepared pull request and record every lookup."""

    def __init__(self, summary: PullRequestSummary | None) -> None:
        """Serve ``summary`` and start the call log empty."""
        self._summary = summary
        self.calls: list[tuple[str, str]] = []

    def __call__(self, remote_url: str, branch: str) -> PullRequestSummary | None:
        """Record the lookup and return the prepared summary."""
        self.calls.append((remote_url, branch))
        return self._summary


class FakeTargetEnumerator:
    """Return a prepared target availability and record every read."""

    def __init__(self, available: frozenset[str] | HookError) -> None:
        """Serve ``available`` and start the call counter at zero."""
        self._available = available
        self.calls = 0

    def __call__(self, repo: Path, driver: object) -> frozenset[str] | HookError:
        """Count the read and return the prepared availability."""
        del repo, driver
        self.calls += 1
        return self._available


def gate_ports(
    branch_state: FakeBranchState | None = None,
    *,
    summary: PullRequestSummary | None = None,
    available: frozenset[str] | HookError | None = None,
) -> BranchGatePorts:
    """Build the ports a branch-gate unit test supplies."""
    return BranchGatePorts(
        branch_state=branch_state or FakeBranchState(),
        lookup_pull_request=FakePullRequestLookup(summary),
        enumerate_build_targets=FakeTargetEnumerator(
            frozenset() if available is None else available
        ),
    )


def pull_request(base_oid: str = "remote") -> PullRequestSummary:
    """Return the pull request the rebase tests expect to locate."""
    return PullRequestSummary(number=1, base_branch="main", base_oid=base_oid)
