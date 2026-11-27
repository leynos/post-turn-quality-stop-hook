"""Shared fixtures for quality-stage and pipeline tests."""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook import execution as exec_mod
from post_turn_quality_stop_hook.driver import TargetEnumeration
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.git_facts import GitFacts

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.repository_port import RepositoryOperations

GIT_FACTS = GitFacts(
    primary_remote="origin",
    upstream_ref="origin/feature",
    pr_base_local_ref=None,
    local_base_commit=None,
    three_way_merge_is_configured=False,
)


def command(
    kind: str = "code",
    exit_code: int = 0,
    cmd: str = "make lint",
    *,
    launched: bool = True,
) -> exec_mod.CommandResult:
    """Build a command result for a quality evaluation.

    ``launched`` distinguishes a command that returned its own status from one
    whose executable never started.
    """
    return exec_mod.CommandResult(
        kind=kind,
        cmd=cmd,
        exit_code=exit_code,
        stdout="" if exit_code == 0 else "boom",
        stderr="",
        launched=launched,
    )


class FakeRepositoryOperations:
    """Answer the repository-operations port from prepared values.

    Every answer is an attribute so a test states only what it cares about,
    and ``calls`` records each question asked. Preparation asks
    ``changed_paths_since``; the uncommitted gate asks ``current_branch`` and
    then ``has_uncommitted_work``, so the log is how a test proves an avoided
    query, such as a dirtiness read on a protected branch, never happened.
    """

    def __init__(  # ruff:ignore[too-many-arguments]  # each prepared answer is an independent input.
        self,
        *,
        changed: tuple[str, ...] | None = (),
        changed_error: HookError | None = None,
        dirty: bool | None = False,
        dirty_error: HookError | None = None,
        branch: str | None = "feature",
        branch_error: HookError | None = None,
    ) -> None:
        """Store the answers and start the question log empty."""
        self.changed = changed
        self.changed_error = changed_error
        self.dirty = dirty
        self.dirty_error = dirty_error
        self.branch = branch
        self.branch_error = branch_error
        self.calls: list[str] = []
        self.bases: list[str] = []

    def changed_paths_since(
        self, base_commit: str
    ) -> tuple[tuple[str, ...] | None, HookError | None]:
        """Return the prepared paths and record the base commit asked about."""
        self.calls.append("changed_paths_since")
        self.bases.append(base_commit)
        return self.changed, self.changed_error

    def has_uncommitted_work(self) -> tuple[bool | None, HookError | None]:
        """Return the prepared dirtiness answer."""
        self.calls.append("has_uncommitted_work")
        return self.dirty, self.dirty_error

    def current_branch(self) -> tuple[str | None, HookError | None]:
        """Return the prepared branch answer."""
        self.calls.append("current_branch")
        return self.branch, self.branch_error


class UnusedRepositoryOperations:
    """Repository operations for a path that must ask no question at all.

    A fake that merely returns prepared values would hide a spurious call.
    This double fails instead, so a test can assert that preparation stopped
    before it needed changed paths, or that no gate-time repository query ran.
    """

    _MESSAGE = "the repository-operations port must not be consulted"

    def changed_paths_since(
        self, base_commit: str
    ) -> tuple[tuple[str, ...] | None, HookError | None]:
        """Fail: this path must not ask for changed paths."""
        del base_commit
        raise AssertionError(self._MESSAGE)

    def has_uncommitted_work(self) -> tuple[bool | None, HookError | None]:
        """Fail: this path must not ask about uncommitted work."""
        raise AssertionError(self._MESSAGE)

    def current_branch(self) -> tuple[str | None, HookError | None]:
        """Fail: this path must not ask for the current branch."""
        raise AssertionError(self._MESSAGE)


class ScriptedRepositoryOperationsFactory:
    """Build one prepared port per resolved root and record every root.

    The pipeline builds operations twice on a successful run: preparation
    binds them to ask which paths changed, and the gate phase binds them again
    from the prepared root. Returning the same double keeps both question logs
    in one place, and ``roots`` proves the resolved root is what reached the
    factory.
    """

    def __init__(self, operations: RepositoryOperations) -> None:
        """Serve ``operations`` for every root."""
        self._operations = operations
        self.roots: list[Path] = []

    def __call__(self, repo: Path) -> RepositoryOperations:
        """Record the root and return the prepared port."""
        self.roots.append(repo)
        return self._operations


class UnusedRepositoryOperationsFactory:
    """Fail when a run tries to build repository operations at all."""

    _MESSAGE = "the repository-operations factory must not run"

    def __call__(self, repo: Path) -> RepositoryOperations:
        """Fail: this path must stop before it needs the port."""
        del repo
        raise AssertionError(self._MESSAGE)


def repository_factory(
    operations: RepositoryOperations,
) -> ScriptedRepositoryOperationsFactory:
    """Return a factory that serves ``operations`` and records its roots."""
    return ScriptedRepositoryOperationsFactory(operations)


class UnusedRepositoryFacts:
    """Repository-fact source for a run that must not observe any facts.

    The pipeline skips the observation when the run's observer keeps no
    facts, so a query reaching this source means that short circuit regressed.
    Tests that exercise a recording observer supply a source of their own.
    """

    _MESSAGE = "the repository-facts source must not be queried"

    def current_branch(self, repo: Path) -> tuple[str | None, HookError | None]:
        """Fail: a run with logging disabled must not ask for facts."""
        del repo
        raise AssertionError(self._MESSAGE)

    def head_commit(self, repo: Path) -> tuple[str | None, HookError | None]:
        """Fail: a run with logging disabled must not ask for facts."""
        del repo
        raise AssertionError(self._MESSAGE)

    def has_uncommitted_changes(
        self, repo: Path
    ) -> tuple[bool | None, HookError | None]:
        """Fail: a run with logging disabled must not ask for facts."""
        del repo
        raise AssertionError(self._MESSAGE)

    def is_ahead_of_upstream(
        self, repo: Path, upstream_ref: str
    ) -> tuple[bool | None, HookError | None]:
        """Fail: a run with logging disabled must not ask for facts."""
        del repo, upstream_ref
        raise AssertionError(self._MESSAGE)


def enumerated(
    available: set[str] | HookError,
    *,
    exit_status: int = 0,
    launched: bool = True,
) -> TargetEnumeration:
    """Wrap declared targets in the enumeration result a driver port returns."""
    targets = available if isinstance(available, HookError) else frozenset(available)
    return TargetEnumeration(
        available=targets, exit_status=exit_status, launched=launched
    )


def recording_warn() -> tuple[list[str], object]:
    """Return a warning channel and the list it appends to.

    The observer no longer writes standard error itself, so a test that runs
    the pipeline must supply the channel the composition root would bind.
    """
    messages: list[str] = []

    def warn(message: str) -> None:
        """Keep one warning for direct assertions."""
        messages.append(message)

    return messages, warn
