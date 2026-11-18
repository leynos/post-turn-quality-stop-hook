"""Time sources used to timestamp and time decision-log records."""

from __future__ import annotations

import datetime as dt
import time
import typing as typ


class Clock(typ.Protocol):
    """Time source used to timestamp and time records."""

    def monotonic(self) -> float:
        """Return a monotonic timestamp in seconds."""
        ...

    def wall(self) -> dt.datetime:
        """Return the current timezone-aware UTC time."""
        ...


class SystemClock:
    """Clock backed by the operating system."""

    @staticmethod
    def monotonic() -> float:
        """Return a monotonic timestamp in seconds."""
        return time.monotonic()

    @staticmethod
    def wall() -> dt.datetime:
        """Return the current timezone-aware UTC time."""
        return dt.datetime.now(dt.UTC)


def _elapsed_ms(started: float, current: float) -> int:
    """Return whole milliseconds between two monotonic timestamps."""
    return max(0, int((current - started) * 1000))
