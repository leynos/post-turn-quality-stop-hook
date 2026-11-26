"""Domain-facing ports the branch-state and pull-request gates decide from.

The gates in :mod:`.branch_gates` import this module only. A branch-state port
supplies the Git questions the gates ask, which keeps the decision path free of
the Git adapter, and the pull-request lookup and build-target enumerator arrive
as callables bound at the composition root. A test therefore decides every gate
from prepared answers, with no Git, GitHub, subprocess, or template rendering.

The types here carry no transport behaviour. ``GitBranchState`` in
:mod:`.branch_state_git` is the production implementation, and the entry point
is the only place that builds it.
"""

from __future__ import annotations

import dataclasses
import typing as typ

from post_turn_quality_stop_hook.errors import HookError

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.driver import BuildDriver
    from post_turn_quality_stop_hook.fetch_port import FetchReporter
    from post_turn_quality_stop_hook.pull_request import PullRequestSummary

#: Declared build-target names, or the failure that prevented reading them.
#: The rebase gate needs only this much of an enumeration: whether a
#: ``typecheck`` target exists, and what went wrong when the read failed.
TargetAvailability = frozenset[str] | HookError


class BranchState(typ.Protocol):
    """The Git branch-state questions the gates ask, as one port.

    Every method mirrors the Git adapter's answer shape: a value, or ``None``
    with a :class:`~.errors.HookError` when the question could not be answered.
    A gate reads those answers and never imports the adapter that produced
    them.
    """

    def current_branch(self, repo: Path) -> tuple[str | None, HookError | None]:
        """Return the checked-out branch, or ``None`` for a detached HEAD."""
        ...

    def head_commit(self, repo: Path) -> tuple[str | None, HookError | None]:
        """Return the commit ``HEAD`` names, or an error."""
        ...

    def ensure_remote_branch_ref(  # ruff:ignore[too-many-arguments]  # mirrors the Git helper the port exposes.
        self,
        repo: Path,
        remote: str,
        branch: str,
        *,
        always_fetch: bool,
        observer: FetchReporter | None = None,
    ) -> tuple[bool, HookError | None, bool]:
        """Ensure ``refs/remotes/<remote>/<branch>`` exists, fetching if asked."""
        ...

    def has_uncommitted_changes(
        self, repo: Path
    ) -> tuple[bool | None, HookError | None]:
        """Return whether the working tree has uncommitted changes."""
        ...

    def merge_base(
        self, repo: Path, ref: str, *, commit: str = "HEAD"
    ) -> tuple[str | None, HookError | None]:
        """Return the merge base of ``ref`` and ``commit``."""
        ...

    def is_ancestor(
        self, repo: Path, ancestor: str, descendant: str
    ) -> tuple[bool | None, HookError | None]:
        """Return whether one commit is an ancestor of another."""
        ...

    def remote_url(
        self, repo: Path, remote: str
    ) -> tuple[str | None, HookError | None]:
        """Return the configured URL of ``remote``."""
        ...


class PullRequestLookup(typ.Protocol):
    """Locate the open pull request whose head is a branch.

    The implementation binds its own timeout, so the gate never reads a
    configuration value to perform the lookup.
    """

    def __call__(self, remote_url: str, branch: str) -> PullRequestSummary | None:
        """Return the open pull request, or ``None`` when none can be read."""
        ...


class BuildTargetEnumerator(typ.Protocol):
    """Read the build targets a driver declares.

    The rebase gate uses the result only to decide whether a ``typecheck``
    target exists and whether the read failed, so the port returns the
    availability rather than the full enumeration.
    """

    def __call__(self, repo: Path, driver: BuildDriver) -> TargetAvailability:
        """Return the declared targets, or the failure that prevented the read."""
        ...


@dataclasses.dataclass(slots=True, frozen=True)
class BranchGatePorts:
    """Adapters the branch-state gates receive from the composition root.

    Attributes
    ----------
    branch_state
        Git branch-state questions the gates ask.
    lookup_pull_request
        Open-pull-request lookup with its timeout already bound.
    enumerate_build_targets
        Declared build-target reader for the selected driver.

    """

    branch_state: BranchState
    lookup_pull_request: PullRequestLookup
    enumerate_build_targets: BuildTargetEnumerator
