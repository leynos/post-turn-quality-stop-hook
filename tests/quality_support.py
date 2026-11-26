"""Shared fixtures for quality-stage and pipeline tests."""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook import execution as exec_mod
from post_turn_quality_stop_hook.driver import TargetEnumeration
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.git_facts import GitFacts

if typ.TYPE_CHECKING:
    from pathlib import Path

GIT_FACTS = GitFacts(
    primary_remote="origin",
    upstream_ref="origin/feature",
    pr_base_local_ref=None,
    local_base_commit=None,
    three_way_merge_is_configured=False,
)


def command(
    kind: str = "code", exit_code: int = 0, cmd: str = "make lint"
) -> exec_mod.CommandResult:
    """Build a command result for a quality evaluation."""
    return exec_mod.CommandResult(
        kind=kind,
        cmd=cmd,
        exit_code=exit_code,
        stdout="" if exit_code == 0 else "boom",
        stderr="",
    )


class UnusedRepositoryFacts:
    """Repository-fact source for a run that must not observe any facts.

    The pipeline skips the observation when the run's observer keeps no
    facts, so a query reaching this source means that short circuit regressed.
    Tests that exercise a recording observer supply a source of their own.
    """

    _MESSAGE = "the repository-facts source must not be queried"

    def current_branch(self, repo: Path) -> str | None:
        """Fail: a run with logging disabled must not ask for facts."""
        del repo
        raise AssertionError(self._MESSAGE)

    def head_commit(self, repo: Path) -> str | None:
        """Fail: a run with logging disabled must not ask for facts."""
        del repo
        raise AssertionError(self._MESSAGE)

    def has_uncommitted_changes(self, repo: Path) -> bool | None:
        """Fail: a run with logging disabled must not ask for facts."""
        del repo
        raise AssertionError(self._MESSAGE)

    def is_ahead_of_upstream(self, repo: Path, upstream_ref: str) -> bool | None:
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
