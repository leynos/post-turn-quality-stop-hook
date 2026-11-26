"""Record shapes and collaborator protocols for the decision log."""

from __future__ import annotations

import dataclasses
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path


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


class RecordTooLargeError(ValueError):
    """A completed record exceeds the writer's per-line bound.

    The writer drops such a record rather than appending it: a line over the
    ownership reader's limit would make the whole daily file foreign to every
    later run and to retention. The error carries no text, so the storage
    telemetry can record the closed ``RecordTooLarge`` category alone.
    """


class RecordSink(typ.Protocol):
    """Persistence boundary for completed decision-log records."""

    def write(self, record: cabc.Mapping[str, object]) -> None:
        """Append one completed record."""
        ...

    def prune(self, today: dt.date, retention_days: int) -> None:
        """Remove records older than ``retention_days``."""
        ...


@dataclasses.dataclass(slots=True, frozen=True)
class PruneReport:
    """What one retention sweep did, without any storage measurement itself.

    Attributes
    ----------
    removed
        Number of expired files deleted.
    failures
        Number of scan or deletion failures the sweep reported. The measurement
        wrapper counts each one, so the storage record keeps describing the
        sweep's failures even though the sink owns no counters.

    """

    removed: int = 0
    failures: int = 0


class StorageSink(typ.Protocol):
    """Storage unit a measurement wrapper times and counts.

    It reports the lock wait an append incurred and what a prune did, so the
    wrapper can record those figures without owning any file.
    """

    def write(self, record: cabc.Mapping[str, object]) -> int:
        """Append one completed record and return its lock wait in milliseconds."""
        ...

    def prune(self, today: dt.date, retention_days: int) -> PruneReport:
        """Remove records older than ``retention_days`` and report the sweep."""
        ...
