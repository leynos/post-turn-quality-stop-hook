"""Consecutive-failure circuit breaker for the stop hook.

A blocking stop hook sends the agent back to work. When the agent cannot fix
the failure — a flaky remote, a broken toolchain, a gate it has no authority
to satisfy — the hook and agent loop indefinitely. This module counts
consecutive blocking outcomes per working directory and, once the configured
limit is reached, lets the next stop through without running the checks.

The module holds the policy only. ``decide``, ``action_after_reset`` and
``next_streak`` are pure functions. Persistence sits behind the
``StreakStore`` port, and the checks report a ``CheckResult`` rather than
printing hook JSON for the breaker to parse; ``hook.main`` composes the file
adapter and the output adapter around this policy.

Bookkeeping fails open: a store that cannot load or save never changes the
decision the checks reach, it only disables the breaker. That choice is made
here, in ``run_with_failure_limit``, rather than in the adapter.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import logging
import os
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

logger = logging.getLogger(__name__)


class CheckOutcome(enum.Enum):
    """Whether one run of the stop checks blocked the stop."""

    PASSED = enum.auto()
    BLOCKED = enum.auto()


@dataclasses.dataclass(frozen=True, slots=True)
class CheckResult:
    """Exit code and outcome of one run of the stop checks."""

    exit_code: int
    outcome: CheckOutcome


@dataclasses.dataclass(frozen=True, slots=True)
class ShortCircuit:
    """The breaker let the stop through without running the checks.

    ``previous`` is the streak that tripped it; the caller renders the
    explanation for the agent.
    """

    previous: int


class BreakerAction(enum.Enum):
    """What the breaker does before the checks run."""

    RUN_CHECKS = enum.auto()
    RESET = enum.auto()
    SHORT_CIRCUIT = enum.auto()


class StreakStoreError(Exception):
    """A streak store could not load, save or clear a streak."""


class StreakStore(typ.Protocol):
    """Port for the per-directory count of consecutive blocks.

    Implementations raise ``StreakStoreError`` when the backing storage
    fails, and report a directory with no recorded streak as zero.
    """

    def load(self, directory: Path) -> int:
        """Return the recorded consecutive-block count for ``directory``."""
        ...

    def save(self, directory: Path, count: int) -> None:
        """Record ``count`` consecutive blocks for ``directory``."""
        ...

    def clear(self, directory: Path) -> None:
        """Forget any streak recorded for ``directory``."""
        ...


def streak_key(directory: Path) -> str:
    """Return a stable, separator-free identity for ``directory``.

    Hashing keeps the identity short and keeps the raw path out of logs. The
    filesystem codec round-trips surrogate-escaped bytes from non-UTF-8 POSIX
    names, which strict UTF-8 encoding would reject.
    """
    return hashlib.sha256(os.fsencode(directory)).hexdigest()


def decide(previous: int, limit: int) -> BreakerAction:
    """Choose the action before the checks run.

    A non-positive ``limit`` disables the breaker. Reaching the limit asks for
    a reset rather than a short circuit: see ``action_after_reset``.
    """
    if limit > 0 and previous >= limit:
        return BreakerAction.RESET
    return BreakerAction.RUN_CHECKS


def action_after_reset(*, is_reset_recorded: bool) -> BreakerAction:
    """Short-circuit only once the reset has stuck.

    The reset is what makes checks resume on the next stop. If it failed,
    skipping now would leave the tripped count in place and skip every later
    stop too, so the checks run instead.
    """
    if is_reset_recorded:
        return BreakerAction.SHORT_CIRCUIT
    return BreakerAction.RUN_CHECKS


def next_streak(previous: int, outcome: CheckOutcome) -> int:
    """Return the streak after a run: one more on a block, otherwise zero."""
    if outcome is CheckOutcome.BLOCKED:
        return previous + 1
    return 0


def run_with_failure_limit(
    directory: Path,
    limit: int,
    run: cabc.Callable[[], CheckResult],
    store: StreakStore,
) -> CheckResult | ShortCircuit:
    """Run the stop checks unless ``directory`` has blocked ``limit`` times.

    Parameters
    ----------
    directory
        Working directory that identifies the streak.
    limit
        Consecutive blocks tolerated before the next stop is let through.
        Zero disables the breaker and leaves the store untouched.
    run
        Stop checks to run, reporting their exit code and outcome.
    store
        Port that persists the streak between hook processes.

    Returns
    -------
    CheckResult | ShortCircuit
        The checks' result, or ``ShortCircuit`` when the checks were skipped.

    """
    if limit <= 0:
        return run()

    key = streak_key(directory)
    previous = _load(store, directory, key)
    action = decide(previous, limit)
    if action is BreakerAction.RESET:
        action = action_after_reset(is_reset_recorded=_record(store, directory, key, 0))
    if action is BreakerAction.SHORT_CIRCUIT:
        logger.warning(
            "failure breaker tripped: limit=%d previous=%d streak=%s "
            "action=reset_and_allow",
            limit,
            previous,
            key,
        )
        return ShortCircuit(previous)

    result = run()
    _record(store, directory, key, next_streak(previous, result.outcome))
    return result


def _load(store: StreakStore, directory: Path, key: str) -> int:
    """Load the streak, treating an unavailable store as no streak."""
    try:
        return store.load(directory)
    except StreakStoreError as exc:
        logger.warning(
            "failure streak unavailable: operation=load streak=%s error=%s", key, exc
        )
        return 0


def _record(store: StreakStore, directory: Path, key: str, count: int) -> bool:
    """Persist ``count``, clearing the streak at zero; report whether it stuck.

    Increments and resets are routine, so they log at ``info``, which the hook
    does not emit by default; only a failed write is a warning.
    """
    operation = "save" if count else "clear"
    try:
        if count:
            store.save(directory, count)
        else:
            store.clear(directory)
    except StreakStoreError as exc:
        logger.warning(
            "failure streak unavailable: operation=%s streak=%s error=%s",
            operation,
            key,
            exc,
        )
        return False
    logger.info(
        "failure streak recorded: operation=%s streak=%s count=%d",
        operation,
        key,
        count,
    )
    return True
