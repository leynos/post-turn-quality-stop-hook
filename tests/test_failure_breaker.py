"""Unit tests for the consecutive-failure breaker policy.

The policy is exercised without a filesystem or environment: decisions are
pure functions, and ``run_with_failure_limit`` runs against an in-memory
``StreakStore``. The file adapter is tested in ``test_streak_store``.
"""

from __future__ import annotations

import logging
import os
import typing as typ
from pathlib import Path

import pytest

from post_turn_quality_stop_hook import failure_breaker as fb
from post_turn_quality_stop_hook.failure_breaker import (
    BreakerAction,
    CheckOutcome,
    CheckResult,
    ShortCircuit,
    StreakStoreError,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

LIMIT = 3
SMALL_LIMIT = 2
SENTINEL_EXIT_CODE = 3
DIRECTORY = Path("/work/repo")
BLOCKED = CheckResult(exit_code=0, outcome=CheckOutcome.BLOCKED)
PASSED = CheckResult(exit_code=0, outcome=CheckOutcome.PASSED)


class InMemoryStore:
    """Hold streaks in a dictionary, optionally failing chosen operations."""

    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        """Start empty; each operation named in ``failing`` raises."""
        self.streaks: dict[Path, int] = {}
        self.failing = failing

    def _check(self, operation: str) -> None:
        """Raise for an operation configured to fail."""
        if operation in self.failing:
            message = f"{operation} refused"
            raise StreakStoreError(message)

    def load(self, directory: Path) -> int:
        """Return the stored streak, or 0."""
        self._check("load")
        return self.streaks.get(directory, 0)

    def save(self, directory: Path, count: int) -> None:
        """Store ``count`` for ``directory``."""
        self._check("save")
        self.streaks[directory] = count

    def clear(self, directory: Path) -> None:
        """Forget ``directory``."""
        self._check("clear")
        self.streaks.pop(directory, None)


class _CountingRun:
    """Return a fixed result and record how often the checks ran."""

    def __init__(self, result: CheckResult) -> None:
        """Wrap ``result`` with a zero call count."""
        self.calls = 0
        self.result = result

    def __call__(self) -> CheckResult:
        """Count the call and return the fixed result."""
        self.calls += 1
        return self.result


def _run_times(
    times: int,
    run: cabc.Callable[[], CheckResult],
    store: InMemoryStore,
    limit: int = LIMIT,
) -> list[CheckResult | ShortCircuit]:
    """Invoke the breaker ``times`` times for ``DIRECTORY``."""
    return [
        fb.run_with_failure_limit(DIRECTORY, limit, run, store) for _ in range(times)
    ]


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
        ("previous", "outcome", "expected"),
        [
            (0, CheckOutcome.BLOCKED, 1),
            (LIMIT, CheckOutcome.BLOCKED, LIMIT + 1),
            (LIMIT, CheckOutcome.PASSED, 0),
            (0, CheckOutcome.PASSED, 0),
        ],
    )
    def test_next_streak(
        self, previous: int, outcome: CheckOutcome, expected: int
    ) -> None:
        """A block extends the streak; a pass resets it."""
        assert fb.next_streak(previous, outcome) == expected


class TestStreakKey:
    """Directory identities are stable hashes."""

    def test_key_is_stable_and_distinct(self) -> None:
        """The same directory maps to one key; others do not collide."""
        assert fb.streak_key(DIRECTORY) == fb.streak_key(Path("/work/repo"))
        assert fb.streak_key(DIRECTORY) != fb.streak_key(Path("/work/other"))

    def test_undecodable_directory_names_hash(self) -> None:
        """Surrogate-escaped bytes from non-UTF-8 names hash without error."""
        directory = Path(os.fsdecode(b"/work/caf\xe9"))
        assert len(fb.streak_key(directory)) == len(fb.streak_key(DIRECTORY))


class TestRunWithFailureLimit:
    """Short-circuit the checks after repeated blocks in one directory."""

    def test_counts_blocks_and_short_circuits_at_the_limit(self) -> None:
        """After ``limit`` blocks the next run skips the checks."""
        store = InMemoryStore()
        run = _CountingRun(BLOCKED)

        results = _run_times(LIMIT + 1, run, store)

        assert results[:-1] == [BLOCKED] * LIMIT
        assert results[-1] == ShortCircuit(previous=LIMIT)
        assert run.calls == LIMIT, "the short-circuited run must not execute checks"

    def test_short_circuit_resets_the_streak(self) -> None:
        """Checks resume after a short circuit rather than staying disabled."""
        store = InMemoryStore()
        run = _CountingRun(BLOCKED)

        results = _run_times(SMALL_LIMIT + 2, run, store, SMALL_LIMIT)

        assert results[-1] == BLOCKED
        assert run.calls == SMALL_LIMIT + 1
        assert store.streaks == {DIRECTORY: 1}

    def test_success_clears_the_streak(self) -> None:
        """A passing run breaks the chain of consecutive failures."""
        store = InMemoryStore()
        _run_times(1, _CountingRun(BLOCKED), store, SMALL_LIMIT)
        _run_times(1, _CountingRun(PASSED), store, SMALL_LIMIT)

        results = _run_times(SMALL_LIMIT, _CountingRun(BLOCKED), store, SMALL_LIMIT)

        assert results == [BLOCKED] * SMALL_LIMIT
        assert store.streaks == {DIRECTORY: SMALL_LIMIT}

    def test_directories_are_counted_independently(self) -> None:
        """A streak in one directory does not short-circuit another."""
        store = InMemoryStore()
        run = _CountingRun(BLOCKED)
        directories = [Path("/work/a"), Path("/work/b")]

        results = [
            fb.run_with_failure_limit(directory, 1, run, store)
            for directory in directories
        ]

        assert results == [BLOCKED, BLOCKED]
        assert store.streaks == dict.fromkeys(directories, 1)

    def test_zero_limit_disables_the_breaker(self) -> None:
        """A limit of zero never short-circuits and never touches the store."""
        store = InMemoryStore(failing=frozenset({"load", "save", "clear"}))
        run = _CountingRun(BLOCKED)

        results = _run_times(LIMIT, run, store, 0)

        assert results == [BLOCKED] * LIMIT
        assert run.calls == LIMIT

    def test_passes_the_result_through(self) -> None:
        """The checks' exit code and outcome come back unchanged."""
        result = CheckResult(exit_code=SENTINEL_EXIT_CODE, outcome=CheckOutcome.PASSED)

        assert _run_times(1, _CountingRun(result), InMemoryStore()) == [result]

    @pytest.mark.parametrize("operation", ["load", "save"])
    def test_an_unavailable_store_does_not_change_the_decision(
        self, operation: str
    ) -> None:
        """Bookkeeping failures fail open: the block is still reported."""
        store = InMemoryStore(failing=frozenset({operation}))
        run = _CountingRun(BLOCKED)

        results = _run_times(LIMIT + 1, run, store)

        assert results == [BLOCKED] * (LIMIT + 1)
        assert run.calls == LIMIT + 1

    def test_an_unclearable_streak_runs_the_checks_instead_of_skipping(self) -> None:
        """Without a successful reset, a tripped breaker would skip every stop."""
        store = InMemoryStore(failing=frozenset({"clear"}))
        store.streaks[DIRECTORY] = LIMIT
        run = _CountingRun(BLOCKED)

        results = _run_times(SMALL_LIMIT, run, store)

        assert results == [BLOCKED] * SMALL_LIMIT
        assert run.calls == SMALL_LIMIT, "an unclearable streak must not skip checks"

    def test_tripping_logs_a_structured_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The trip is logged with its limit, streak and hashed directory."""
        store = InMemoryStore()
        store.streaks[DIRECTORY] = LIMIT

        with caplog.at_level(logging.WARNING, logger=fb.__name__):
            _run_times(1, _CountingRun(BLOCKED), store)

        (record,) = caplog.records
        assert record.levelno == logging.WARNING
        assert record.getMessage() == (
            f"failure breaker tripped: limit={LIMIT} previous={LIMIT} "
            f"streak={fb.streak_key(DIRECTORY)} action=reset_and_allow"
        )
        assert str(DIRECTORY) not in record.getMessage()

    def test_store_failures_log_the_operation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed write is a warning naming the operation that failed."""
        store = InMemoryStore(failing=frozenset({"save"}))

        with caplog.at_level(logging.WARNING, logger=fb.__name__):
            _run_times(1, _CountingRun(BLOCKED), store)

        (record,) = caplog.records
        assert "operation=save" in record.getMessage()
        assert "save refused" in record.getMessage()
