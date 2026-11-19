"""Record shapes and collaborator protocols for the decision log."""

from __future__ import annotations

import dataclasses
import typing as typ

from post_turn_quality_stop_hook.decision_log.constants import (
    MAX_CORRELATION_CHARS,
    MAX_REASON_CHARS,
)
from post_turn_quality_stop_hook.diagnostics import warn
from post_turn_quality_stop_hook.git import is_tracked as git_is_tracked
from post_turn_quality_stop_hook.git import would_appear_as_change

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    from post_turn_quality_stop_hook.decision_log.clock import Clock
    from post_turn_quality_stop_hook.decision_log.stats import StorageStats


def _warn(message: str) -> None:
    """Write a warning to standard error without risking the hook decision.

    Kept as a module-level name because callers and tests pass it as an error
    sink; :func:`post_turn_quality_stop_hook.diagnostics.warn` holds the guard
    that keeps a missing or closed stream from reaching standard output.
    """
    warn(message)


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


@dataclasses.dataclass(slots=True, frozen=True)
class InvocationContext:
    """Facts that identify and locate one hook invocation.

    Attributes
    ----------
    correlation
        Hook correlation fields such as ``session_id`` and
        ``transcript_path``.
    start_dir
        Directory where the hook started.
    base_ref
        Base ref used for repository comparison.

    """

    correlation: cabc.Mapping[str, object]
    start_dir: Path
    base_ref: str


class RecordSink(typ.Protocol):
    """Persistence boundary for completed decision-log records."""

    def write(self, record: cabc.Mapping[str, object]) -> None:
        """Append one completed record."""
        ...

    def prune(self, today: dt.date, retention_days: int) -> None:
        """Remove records older than ``retention_days``."""
        ...


@dataclasses.dataclass(slots=True)
class DecisionLogDeps:
    """Collaborators that a caller may substitute for the defaults.

    Attributes
    ----------
    sink
        Persistence adapter. Defaults to a JSONL sink beneath ``state_home``.
    clock
        Time source. Defaults to the system clock.
    run_id_factory
        Identifier source. Defaults to a random UUID hex string.
    is_tracked
        Git ownership query used by the default sink. Defaults to
        :func:`post_turn_quality_stop_hook.git.is_tracked`, so the sink never
        turns a committed daily file into an uncommitted change.
    is_visible
        Query used by the default sink for whether a worktree could report a
        daily file as an untracked change. Defaults to
        :func:`post_turn_quality_stop_hook.git.would_appear_as_change`, so the
        sink writes only where the gates cannot see the file.
    storage
        Telemetry collector the log writes its storage record from, and the
        default sink records into. A substituted sink keeps its own unless the
        caller supplies a collector, so the log never reads one back from the
        sink.

    """

    sink: RecordSink | None = None
    clock: Clock | None = None
    run_id_factory: cabc.Callable[[], str] | None = None
    is_tracked: cabc.Callable[[Path], bool | None] = git_is_tracked
    is_visible: cabc.Callable[[Path], bool | None] = would_appear_as_change
    storage: StorageStats | None = None
