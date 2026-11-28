"""Consecutive-failure circuit breaker for the stop hook.

A blocking stop hook sends the agent back to work. When the agent cannot fix
the failure — a flaky remote, a broken toolchain, a gate it has no authority
to satisfy — the hook and agent loop indefinitely. This module counts
consecutive blocking outcomes per streak and, once the configured limit is
reached, lets the next stop through without running the checks.

The module holds the policy only. ``decide``, ``action_after_reset`` and
``update_for`` are pure functions. A streak is named by an opaque
``StreakId``; the composition root derives it from the working directory, so
the policy knows nothing of paths or encodings. Persistence sits behind the
``StreakStore`` port, whose operations are atomic per streak, and the checks
report a ``CheckResult`` rather than printing hook JSON for the breaker to
parse.

Bookkeeping fails open: a store that cannot load or record never changes the
decision the checks reach, it only disables the breaker. That choice is made
here, in ``run_with_failure_limit``, rather than in the adapter.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

logger = logging.getLogger(__name__)

StreakId = typ.NewType("StreakId", str)
"""Opaque, log-safe identity of one streak, such as a hashed directory."""


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


class StreakUpdate(enum.Enum):
    """How a run of the checks changes the stored streak."""

    INCREMENT = enum.auto()
    CLEAR = enum.auto()


class StreakStoreError(Exception):
    """A streak store could not load or record a streak.

    Messages name the failed operation and the error kind only, never a path,
    because the breaker logs them.
    """


class StreakStore(typ.Protocol):
    """Port for the count of consecutive blocks in each streak.

    Each mutating operation is atomic for its streak, so concurrent hook
    processes neither lose updates nor both reset one tripped streak.
    Implementations raise ``StreakStoreError`` when the backing storage
    fails, and report a streak with no record as zero.
    """

    def load(self, streak: StreakId) -> int:
        """Return the recorded consecutive-block count for ``streak``."""
        ...

    def increment(self, streak: StreakId) -> int:
        """Add one to the stored count for ``streak`` and return the result."""
        ...

    def clear(self, streak: StreakId) -> None:
        """Forget any count recorded for ``streak``."""
        ...

    def reset_if_tripped(self, streak: StreakId, limit: int) -> bool:
        """Clear ``streak`` only if its count still reaches ``limit``.

        Returns whether this call performed the reset. ``False`` means
        another process reset or changed the streak first.
        """
        ...


def decide(previous: int, limit: int) -> BreakerAction:
    """Choose the action before the checks run.

    A non-positive ``limit`` disables the breaker. Reaching the limit asks for
    a reset rather than a short circuit: see ``action_after_reset``.
    """
    if limit > 0 and previous >= limit:
        return BreakerAction.RESET
    return BreakerAction.RUN_CHECKS


def action_after_reset(*, is_reset_recorded: bool) -> BreakerAction:
    """Short-circuit only once this process's reset has stuck.

    The reset is what makes checks resume on the next stop. If it failed,
    skipping now would leave the tripped count in place and skip every later
    stop too. If another process reset the streak first, that process owns
    the short circuit. Either way the checks run instead.
    """
    if is_reset_recorded:
        return BreakerAction.SHORT_CIRCUIT
    return BreakerAction.RUN_CHECKS


def update_for(outcome: CheckOutcome) -> StreakUpdate:
    """Return the streak update for a run: a block extends, a pass clears."""
    if outcome is CheckOutcome.BLOCKED:
        return StreakUpdate.INCREMENT
    return StreakUpdate.CLEAR


def run_with_failure_limit(
    streak: StreakId,
    limit: int,
    run: cabc.Callable[[], CheckResult],
    store: StreakStore,
) -> CheckResult | ShortCircuit:
    """Run the stop checks unless ``streak`` has blocked ``limit`` times.

    Parameters
    ----------
    streak
        Opaque identity of the streak, such as a hashed working directory.
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

    previous = _load(store, streak)
    action = decide(previous, limit)
    if action is BreakerAction.RESET:
        action = action_after_reset(
            is_reset_recorded=_reset_if_tripped(store, streak, limit)
        )
    if action is BreakerAction.SHORT_CIRCUIT:
        logger.warning(
            "failure breaker tripped: limit=%d previous=%d streak=%s "
            "action=reset_and_allow",
            limit,
            previous,
            streak,
        )
        return ShortCircuit(previous)

    result = run()
    _record(store, streak, update_for(result.outcome))
    return result


def _warn_unavailable(operation: str, streak: StreakId, exc: StreakStoreError) -> None:
    """Log a store failure; the error text carries no path by contract."""
    logger.warning(
        "failure streak unavailable: operation=%s streak=%s error=%s",
        operation,
        streak,
        exc,
    )


def _load(store: StreakStore, streak: StreakId) -> int:
    """Load the streak, treating an unavailable store as no streak."""
    try:
        return store.load(streak)
    except StreakStoreError as exc:
        _warn_unavailable("load", streak, exc)
        return 0


def _reset_if_tripped(store: StreakStore, streak: StreakId, limit: int) -> bool:
    """Try to claim the reset; a store failure counts as not reset."""
    try:
        return store.reset_if_tripped(streak, limit)
    except StreakStoreError as exc:
        _warn_unavailable("reset", streak, exc)
        return False


def _record(store: StreakStore, streak: StreakId, update: StreakUpdate) -> None:
    """Apply ``update``, logging rather than raising when the store fails.

    Increments and clears are routine, so they log at ``info``, which the hook
    does not emit by default; only a failed write is a warning.
    """
    try:
        if update is StreakUpdate.INCREMENT:
            count = store.increment(streak)
        else:
            store.clear(streak)
            count = 0
    except StreakStoreError as exc:
        _warn_unavailable(update.name.lower(), streak, exc)
        return
    logger.info(
        "failure streak recorded: operation=%s streak=%s count=%d",
        update.name.lower(),
        streak,
        count,
    )
