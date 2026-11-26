"""Bound record text before it enters a durable record.

The reader that verifies a daily file before appending to it has a per-line
limit, and correlation values are copied into every record of a run. Bounding
the free text and the payload-derived values here keeps that concern out of the
record shapes, which only describe the fields a record carries.
"""

from __future__ import annotations

from post_turn_quality_stop_hook.decision_log.constants import (
    MAX_CORRELATION_CHARS,
    MAX_REASON_CHARS,
)


def _bounded_reason(value: str | None) -> str | None:
    """Collapse and bound free-text reasons before logging them."""
    if value is None:
        return None
    collapsed = " ".join(value.split())
    if len(collapsed) <= MAX_REASON_CHARS:
        return collapsed
    return collapsed[:MAX_REASON_CHARS]


def _bounded_value(value: object) -> object:
    """Bound a payload-derived string before it enters a record.

    Correlation values are copied into every record of the run, so an absurd
    identifier would otherwise push the daily file past the ownership reader's
    per-line limit. That would make a genuine file foreign: later runs would
    refuse to append to it and retention would never remove it. Ordinary
    identifiers stay byte-for-byte; only an outsized value is truncated.
    """
    if not isinstance(value, str) or len(value) <= MAX_CORRELATION_CHARS:
        return value
    return value[:MAX_CORRELATION_CHARS]
