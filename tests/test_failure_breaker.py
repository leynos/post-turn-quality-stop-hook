"""Unit tests for the consecutive-failure breaker policy.

The policy is exercised without a filesystem or environment: decisions are
pure functions, and ``run_with_failure_limit`` runs against an in-memory
``StreakStore``. Property tests live in ``test_failure_breaker_properties``
and the file adapter is tested in ``test_streak_store``.
"""

from __future__ import annotations

import logging
import typing as typ

import pytest
from streak_support import STREAK, CountingRun, InMemoryStore

from post_turn_quality_stop_hook import failure_breaker as fb
from post_turn_quality_stop_hook.failure_breaker import (
    BreakerAction,
    CheckOutcome,
    CheckResult,
    ShortCircuit,
    StreakId,
    StreakUpdate,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

LIMIT = 3
SMALL_LIMIT = 2
SENTINEL_EXIT_CODE = 3
BLOCKED = CheckResult(exit_code=0, outcome=CheckOutcome.BLOCKED)
PASSED = CheckResult(exit_code=0, outcome=CheckOutcome.PASSED)


def _run_times(
    times: int,
    run: cabc.Callable[[], CheckResult],
    store: InMemoryStore,
    limit: int = LIMIT,
) -> list[CheckResult | ShortCircuit]:
    """Invoke the breaker ``times`` times for ``STREAK``."""
    return [fb.run_with_failure_limit(STREAK, limit, run, store) for _ in range(times)]


class TestDecide:
    """The pre-run decision is a pure function of the streak and the limit."""

    @pytest.mark.parametrize(
        ("previous", "limit", "expected"),
        [
            (0, LIMIT, BreakerAction.RUN_CHECKS),
            (LIMIT - 1, LIMIT, BreakerAction.RUN_CHECKS),
            (LIMIT, LIMIT, BreakerAction.RESET),
            (LIMIT + 1, LIMIT, BreakerAction.RESET),
            (LIMIT, 0, BreakerAction.RUN_CHECKS),
            (LIMIT, -1, BreakerAction.RUN_CHECKS),
        ],
    )
    def test_decide(self, previous: int, limit: int, expected: BreakerAction) -> None:
        """Reaching a positive limit asks for a reset; otherwise run checks."""
        assert fb.decide(previous, limit) is expected

    @pytest.mark.parametrize(
        ("is_reset_recorded", "expected"),
        [(True, BreakerAction.SHORT_CIRCUIT), (False, BreakerAction.RUN_CHECKS)],
    )
    def test_short_circuit_only_after_a_recorded_reset(
        self, *, is_reset_recorded: bool, expected: BreakerAction
    ) -> None:
        """An unrecorded reset runs the checks rather than skipping them."""
        assert fb.action_after_reset(is_reset_recorded=is_reset_recorded) is expected

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (CheckOutcome.BLOCKED, StreakUpdate.INCREMENT),
            (CheckOutcome.PASSED, StreakUpdate.CLEAR),
        ],
    )
    def test_update_for(self, outcome: CheckOutcome, expected: StreakUpdate) -> None:
        """A block extends the streak; a pass clears it."""
        assert fb.update_for(outcome) is expected


class TestRunWithFailureLimit:
    """Short-circuit the checks after repeated blocks in one streak."""

    def test_counts_blocks_and_short_circuits_at_the_limit(self) -> None:
        """After ``limit`` blocks the next run skips the checks."""
        store = InMemoryStore()
        run = CountingRun(BLOCKED)

        results = _run_times(LIMIT + 1, run, store)

        assert results[:-1] == [BLOCKED] * LIMIT
        assert results[-1] == ShortCircuit(previous=LIMIT)
        assert run.calls == LIMIT, "the short-circuited run must not execute checks"

    def test_short_circuit_resets_the_streak(self) -> None:
        """Checks resume after a short circuit rather than staying disabled."""
        store = InMemoryStore()
        run = CountingRun(BLOCKED)

        results = _run_times(SMALL_LIMIT + 2, run, store, SMALL_LIMIT)

        assert results[-1] == BLOCKED
        assert run.calls == SMALL_LIMIT + 1
        assert store.streaks == {STREAK: 1}

    def test_success_clears_the_streak(self) -> None:
        """A passing run breaks the chain of consecutive failures."""
        store = InMemoryStore()
        _run_times(1, CountingRun(BLOCKED), store, SMALL_LIMIT)
        _run_times(1, CountingRun(PASSED), store, SMALL_LIMIT)

        results = _run_times(SMALL_LIMIT, CountingRun(BLOCKED), store, SMALL_LIMIT)

        assert results == [BLOCKED] * SMALL_LIMIT
        assert store.streaks == {STREAK: SMALL_LIMIT}

    def test_streaks_are_counted_independently(self) -> None:
        """A streak for one directory does not short-circuit another."""
        store = InMemoryStore()
        run = CountingRun(BLOCKED)
        streaks = [StreakId("a"), StreakId("b")]

        results = [fb.run_with_failure_limit(s, 1, run, store) for s in streaks]

        assert results == [BLOCKED, BLOCKED]
        assert store.streaks == dict.fromkeys(streaks, 1)

    def test_zero_limit_disables_the_breaker(self) -> None:
        """A limit of zero never short-circuits and never touches the store."""
        store = InMemoryStore(failing={"load", "increment", "clear", "reset"})
        run = CountingRun(BLOCKED)

        results = _run_times(LIMIT, run, store, 0)

        assert results == [BLOCKED] * LIMIT
        assert run.calls == LIMIT

    def test_passes_the_result_through(self) -> None:
        """The checks' exit code and outcome come back unchanged."""
        result = CheckResult(exit_code=SENTINEL_EXIT_CODE, outcome=CheckOutcome.PASSED)

        assert _run_times(1, CountingRun(result), InMemoryStore()) == [result]

    @pytest.mark.parametrize("operation", ["load", "increment"])
    def test_an_unavailable_store_does_not_change_the_decision(
        self, operation: str
    ) -> None:
        """Bookkeeping failures fail open: the block is still reported."""
        store = InMemoryStore(failing={operation})
        run = CountingRun(BLOCKED)

        results = _run_times(LIMIT + 1, run, store)

        assert results == [BLOCKED] * (LIMIT + 1)
        assert run.calls == LIMIT + 1

    def test_an_unclearable_streak_runs_the_checks_instead_of_skipping(self) -> None:
        """Without a successful reset, a tripped breaker would skip every stop."""
        store = InMemoryStore(failing={"reset"})
        store.streaks[STREAK] = LIMIT
        run = CountingRun(BLOCKED)

        results = _run_times(SMALL_LIMIT, run, store)

        assert results == [BLOCKED] * SMALL_LIMIT
        assert run.calls == SMALL_LIMIT, "an unclearable streak must not skip checks"

    def test_a_reset_claimed_elsewhere_runs_the_checks(self) -> None:
        """When another process resets the streak first, this one runs checks.

        The store refuses the reset because the count no longer reaches the
        limit, so only the process that claimed the reset short-circuits.
        """
        store = InMemoryStore()
        store.streaks[STREAK] = LIMIT
        run = CountingRun(BLOCKED)

        class RacingStore(InMemoryStore):
            """Report the tripped count, then lose the reset to a rival."""

            def reset_if_tripped(self, streak: StreakId, limit: int) -> bool:
                """Let a rival claim the reset before this call runs."""
                assert store.reset_if_tripped(streak, limit)
                return store.reset_if_tripped(streak, limit)

            def load(self, streak: StreakId) -> int:
                """Read through to the shared store."""
                return store.load(streak)

            def increment(self, streak: StreakId) -> int:
                """Write through to the shared store."""
                return store.increment(streak)

        assert _run_times(1, run, RacingStore()) == [BLOCKED]
        assert run.calls == 1

    def test_tripping_logs_a_structured_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The trip is logged with its limit, previous count and streak."""
        store = InMemoryStore()
        store.streaks[STREAK] = LIMIT

        with caplog.at_level(logging.WARNING, logger=fb.__name__):
            _run_times(1, CountingRun(BLOCKED), store)

        (record,) = caplog.records
        assert record.levelno == logging.WARNING
        assert record.getMessage() == (
            f"failure breaker tripped: limit={LIMIT} previous={LIMIT} "
            f"streak={STREAK} action=reset_and_allow"
        )

    @pytest.mark.parametrize(
        ("operation", "result"), [("increment", BLOCKED), ("clear", PASSED)]
    )
    def test_store_failures_log_the_operation(
        self, operation: str, result: CheckResult, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed write is a warning naming the operation that failed."""
        store = InMemoryStore(failing={operation})

        with caplog.at_level(logging.WARNING, logger=fb.__name__):
            _run_times(1, CountingRun(result), store)

        (record,) = caplog.records
        assert record.getMessage() == (
            f"failure streak unavailable: operation={operation} streak={STREAK} "
            f"error=cannot {operation} streak: refused"
        )
