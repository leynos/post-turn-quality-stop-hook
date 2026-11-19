"""Bounded storage telemetry for one invocation's decision log.

The sink records timings and failure categories here rather than keeping them
private. The log then writes the same figures as its own record, so an operator
can tell a slow or contended log from a failing one without any exception text,
path, or command output entering a record. Keeping the counters in a separate
object lets the sink and the log share them without either reading the other's
private state, and lets a test supply its own.
"""

from __future__ import annotations

# Failure labels a record may carry. Exception text can name a path, so only a
# class name is kept, and only from this closed set.
ERROR_CATEGORIES = frozenset({
    "BlockingIOError",
    "FileNotFoundError",
    "IsADirectoryError",
    "NotADirectoryError",
    "OSError",
    "Other",
    "PermissionError",
    "RecordTooLarge",
    "ShortWrite",
    "TimeoutError",
    "UnicodeEncodeError",
    "ValueError",
})


def error_category(
    error: BaseException | None = None, *, fallback: str = "Other"
) -> str:
    """Return a bounded category for a storage failure."""
    if error is None:
        return fallback
    name = type(error).__name__
    return name if name in ERROR_CATEGORIES else fallback


class StorageStats:
    """Accumulate bounded storage timings and failure categories.

    Every method is safe to call from the signal handler path: the object keeps
    only integers and a short allow-listed label set, so it cannot fail on
    encoding and cannot grow with the number of records written.
    """

    def __init__(self) -> None:
        """Start every counter at zero."""
        self._write_attempts = 0
        self._write_failures = 0
        self._write_ms = 0
        self._lock_wait_ms = 0
        self._write_categories: set[str] = set()
        self._prune_attempts = 0
        self._prune_failures = 0
        self._prune_ms = 0
        self._prune_categories: set[str] = set()

    def start_write(self) -> None:
        """Count one attempted record write."""
        self._write_attempts += 1

    def add_write(self, *, storage_ms: int, lock_wait_ms: int) -> None:
        """Accumulate the timings of one completed write attempt."""
        self._write_ms += storage_ms
        self._lock_wait_ms += lock_wait_ms

    def fail_write(self, category: str) -> None:
        """Count one failed write and remember its bounded category."""
        self._write_failures += 1
        self._write_categories.add(category)

    def start_prune(self) -> None:
        """Count one attempted retention pass."""
        self._prune_attempts += 1

    def add_prune(self, *, storage_ms: int) -> None:
        """Accumulate the duration of one retention pass."""
        self._prune_ms += storage_ms

    def fail_prune(self, category: str) -> None:
        """Count one failed retention pass and its bounded category."""
        self._prune_failures += 1
        self._prune_categories.add(category)

    @property
    def attempts(self) -> int:
        """Number of write and retention attempts recorded so far."""
        return self._write_attempts + self._prune_attempts

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Return the counters as the two operation records the log writes.

        The numbers describe storage only. They let an operator tell a slow or
        contended log from a failing one without any exception text, path, or
        command output entering the record.
        """
        return {
            "write": {
                "operation": "write",
                "attempts": self._write_attempts,
                "failures": self._write_failures,
                "storage_ms": self._write_ms,
                "lock_wait_ms": self._lock_wait_ms,
                "error_categories": sorted(self._write_categories),
            },
            "prune": {
                "operation": "prune",
                "attempts": self._prune_attempts,
                "failures": self._prune_failures,
                "storage_ms": self._prune_ms,
                "lock_wait_ms": 0,
                "error_categories": sorted(self._prune_categories),
            },
        }
