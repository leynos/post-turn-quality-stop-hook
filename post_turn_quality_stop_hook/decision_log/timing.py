"""Pure time arithmetic shared by the log's record units.

Timings are measured with an injected :class:`~.clock.Clock`, and the elapsed
value is computed by this module. Keeping the arithmetic separate from the
clock adapter lets a domain unit import it without importing a time source.
"""

from __future__ import annotations


def _elapsed_ms(started: float, current: float) -> int:
    """Return whole milliseconds between two monotonic timestamps."""
    return max(0, int((current - started) * 1000))
