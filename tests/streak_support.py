"""In-memory ``StreakStore`` and run doubles for the failure-breaker tests."""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook.failure_breaker import (
    CheckResult,
    StreakId,
    StreakStoreError,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

STREAK = StreakId("streak-under-test")
OPERATIONS = frozenset({"load", "increment", "clear", "reset"})


class InMemoryStore:
    """Hold streaks in a dictionary, optionally failing chosen operations."""

    def __init__(self, *, failing: cabc.Iterable[str] = ()) -> None:
        """Start empty; each operation named in ``failing`` raises."""
        self.streaks: dict[StreakId, int] = {}
        self.failing = frozenset(failing)

    def _check(self, operation: str) -> None:
        """Raise for an operation configured to fail."""
        if operation in self.failing:
            message = f"cannot {operation} streak: refused"
            raise StreakStoreError(message)

    def load(self, streak: StreakId) -> int:
        """Return the stored streak, or 0."""
        self._check("load")
        return self.streaks.get(streak, 0)

    def increment(self, streak: StreakId) -> int:
        """Add one to the stored streak."""
        self._check("increment")
        self.streaks[streak] = self.streaks.get(streak, 0) + 1
        return self.streaks[streak]

    def clear(self, streak: StreakId) -> None:
        """Forget ``streak``."""
        self._check("clear")
        self.streaks.pop(streak, None)

    def reset_if_tripped(self, streak: StreakId, limit: int) -> int:
        """Clear ``streak`` when it reaches ``limit``; return what was cleared."""
        self._check("reset")
        current = self.streaks.get(streak, 0)
        if current < limit:
            return 0
        del self.streaks[streak]
        return current


class CountingRun:
    """Return a fixed result and record how often the checks ran."""

    def __init__(self, result: CheckResult) -> None:
        """Wrap ``result`` with a zero call count."""
        self.calls = 0
        self.result = result

    def __call__(self) -> CheckResult:
        """Count the call and return the fixed result."""
        self.calls += 1
        return self.result
