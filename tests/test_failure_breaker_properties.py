"""Property tests for the consecutive-failure breaker's transitions.

Each case drives ``run_with_failure_limit`` through a generated sequence of
check outcomes against an in-memory store, optionally with some store
operations failing. The properties state the breaker's contract directly —
how many consecutive blocks precede a short circuit, that checks resume
afterwards, and that storage failures only ever disable the breaker — rather
than replaying the implementation.
"""

from __future__ import annotations

import itertools
import typing as typ

from hypothesis import given
from hypothesis import strategies as st
from streak_support import OPERATIONS, STREAK, InMemoryStore

from post_turn_quality_stop_hook import failure_breaker as fb
from post_turn_quality_stop_hook.failure_breaker import (
    BreakerAction,
    CheckOutcome,
    CheckResult,
    ShortCircuit,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

LIMITS = st.integers(min_value=1, max_value=6)
OUTCOMES = st.lists(st.sampled_from(CheckOutcome), max_size=40)
EXIT_CODES = st.integers(min_value=0, max_value=3)
COUNTS = st.integers(min_value=0, max_value=1_000)


class _Trace(typ.NamedTuple):
    """What happened on each stop: a short circuit or the checks' outcome."""

    events: list[CheckOutcome | ShortCircuit]
    store: InMemoryStore


def _drive(
    limit: int,
    outcomes: cabc.Sequence[CheckOutcome],
    failing: cabc.Iterable[str] = (),
) -> _Trace:
    """Stop repeatedly until every generated outcome has been reported."""
    store = InMemoryStore(failing=failing)
    pending = list(outcomes)
    events: list[CheckOutcome | ShortCircuit] = []

    def run() -> CheckResult:
        """Report the next generated outcome as the checks would."""
        return CheckResult(exit_code=0, outcome=pending.pop(0))

    while pending:
        result = fb.run_with_failure_limit(STREAK, limit, run, store)
        events.append(result if isinstance(result, ShortCircuit) else result.outcome)
    return _Trace(events, store)


def _blocks_before_each_short_circuit(
    events: cabc.Sequence[CheckOutcome | ShortCircuit],
) -> list[int]:
    """Count the consecutive checked blocks that precede each short circuit."""
    counts: list[int] = []
    run_of_blocks = 0
    for event in events:
        if isinstance(event, ShortCircuit):
            counts.append(run_of_blocks)
            run_of_blocks = 0
        elif event is CheckOutcome.BLOCKED:
            run_of_blocks += 1
        else:
            run_of_blocks = 0
    return counts


@given(previous=COUNTS, limit=st.integers(min_value=-3, max_value=50))
def test_decide_resets_exactly_at_a_positive_limit(previous: int, limit: int) -> None:
    """A reset is requested if and only if a positive limit has been reached."""
    expected = limit > 0 and previous >= limit
    assert (fb.decide(previous, limit) is BreakerAction.RESET) is expected


@given(limit=LIMITS, outcomes=OUTCOMES)
def test_short_circuits_follow_exactly_limit_blocks(
    limit: int, outcomes: list[CheckOutcome]
) -> None:
    """Every short circuit follows exactly ``limit`` consecutive blocks.

    No stop sees more than ``limit`` consecutive checked blocks without a
    short circuit, so an agent can never be held for longer than the limit.
    """
    events = _drive(limit, outcomes).events

    assert all(n == limit for n in _blocks_before_each_short_circuit(events))
    run_of_blocks = 0
    for event in events:
        run_of_blocks = run_of_blocks + 1 if event is CheckOutcome.BLOCKED else 0
        assert run_of_blocks <= limit


@given(limit=LIMITS, outcomes=OUTCOMES)
def test_checks_resume_after_every_short_circuit(
    limit: int, outcomes: list[CheckOutcome]
) -> None:
    """A short circuit is always followed by a stop that runs the checks."""
    events = _drive(limit, outcomes).events

    pairs = itertools.pairwise(events)
    assert not any(
        isinstance(a, ShortCircuit) and isinstance(b, ShortCircuit) for a, b in pairs
    )


@given(limit=LIMITS, outcomes=OUTCOMES)
def test_the_store_holds_the_trailing_run_of_blocks(
    limit: int, outcomes: list[CheckOutcome]
) -> None:
    """After any sequence the stored count is the trailing consecutive blocks."""
    trace = _drive(limit, outcomes)

    trailing = 0
    for event in trace.events:
        trailing = trailing + 1 if event is CheckOutcome.BLOCKED else 0
    assert trace.store.streaks.get(STREAK, 0) == trailing


@given(
    limit=LIMITS,
    outcomes=OUTCOMES,
    failing=st.sets(
        st.sampled_from(sorted({"load", "increment", "reset"})), min_size=1
    ),
)
def test_storage_failures_never_skip_the_checks(
    limit: int, outcomes: list[CheckOutcome], failing: set[str]
) -> None:
    """A store that cannot load, count or reset never short-circuits a stop.

    Failing open means the checks' own decision always reaches the agent.
    """
    events = _drive(limit, outcomes, failing).events

    assert events == outcomes


@given(
    limit=st.integers(min_value=-3, max_value=0),
    outcomes=OUTCOMES,
    exit_code=EXIT_CODES,
)
def test_a_disabled_limit_passes_every_result_through(
    limit: int, outcomes: list[CheckOutcome], exit_code: int
) -> None:
    """With the breaker disabled, results pass through and no store is used."""
    store = InMemoryStore(failing=OPERATIONS)

    for outcome in outcomes:
        result = CheckResult(exit_code=exit_code, outcome=outcome)
        assert fb.run_with_failure_limit(STREAK, limit, lambda r=result: r, store) == (
            result
        )
