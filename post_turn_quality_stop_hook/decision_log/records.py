"""Record shapes and collaborator protocols for the decision log."""

from __future__ import annotations

import dataclasses
import typing as typ

from post_turn_quality_stop_hook.decision_log.constants import MAX_REASON_CHARS
from post_turn_quality_stop_hook.diagnostics import warn

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    from post_turn_quality_stop_hook.decision_log.clock import Clock
    from post_turn_quality_stop_hook.decision_log.stats import StorageStats
    from post_turn_quality_stop_hook.git import ExcludedPath


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

    def excluded_paths(self) -> tuple[ExcludedPath, ...] | None:
        """Return entries repository queries must ignore for this run.

        Every daily file this run appended to appears here, whether the run
        created it or verified that an earlier invocation wrote it. Both are
        logging artefacts a logging-disabled run would never have produced, so
        excluding both is what keeps the log observationally transparent. A
        file the hook does not own never appears, so enabling the log cannot
        hide repository content the gates would otherwise report.

        ``None`` reports that the sink could not establish the answer, which
        a caller must surface rather than treat as "nothing to exclude".
        """
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
        Git ownership query used by the default sink. Without it, the sink
        cannot tell whether a daily file belongs to the repository.
    storage
        Telemetry collector the log writes its storage record from, and the
        default sink records into. A substituted sink keeps its own unless the
        caller supplies a collector, so the log never reads one back from the
        sink.

    """

    sink: RecordSink | None = None
    clock: Clock | None = None
    run_id_factory: cabc.Callable[[], str] | None = None
    is_tracked: cabc.Callable[[Path], bool | None] | None = None
    storage: StorageStats | None = None
