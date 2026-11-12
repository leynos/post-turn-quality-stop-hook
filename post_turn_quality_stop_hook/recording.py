"""Domain-shaped recording interface for hook progress and decisions.

The pipeline and gate policy depend on this protocol rather than on the JSONL
persistence adapter. That keeps record collection independent of where and how
records are stored, and lets tests substitute a narrow recorder.
"""

from __future__ import annotations

import dataclasses
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import contextlib

    from post_turn_quality_stop_hook.git import ExcludedPath


@dataclasses.dataclass(slots=True, frozen=True)
class InvocationLog:
    """Recording for one run and the paths its repository queries ignore.

    Attributes
    ----------
    recorder
        Recorder for progress and decisions, or ``None`` when logging is off.

    """

    recorder: DecisionRecorder | None = None

    def excluded_paths(self) -> tuple[ExcludedPath, ...]:
        """Return the log's own entries repository queries must ignore.

        The answer is asked of the recorder at each query rather than captured
        at start, so a run that crosses midnight excludes the second daily
        file too, and a run that wrote nothing excludes nothing.
        """
        return self.recorder.excluded_paths() if self.recorder is not None else ()


@dataclasses.dataclass(slots=True)
class CommandProgress:
    """In-flight command timing used to finish a progress record."""

    kind: str
    cmd: str
    targets: list[str]
    started: float


class DecisionRecorder(typ.Protocol):
    """Record progress, facts, and gate outcomes for one hook invocation."""

    def stage(self, name: str) -> contextlib.AbstractContextManager[None]:
        """Record a stage's start and duration."""
        ...

    def command_start(self, kind: str, cmd: str, targets: list[str]) -> CommandProgress:
        """Record that a blocking command is about to start."""
        ...

    def command_finish(
        self,
        progress: CommandProgress,
        exit_code: int,
        detail: cabc.Mapping[str, object] | None = None,
    ) -> None:
        """Record a blocking command's exit code and duration."""
        ...

    def record_gate(self, gate: str, outcome: str, reason: str | None = None) -> None:
        """Record a gate outcome and a short reason."""
        ...

    def set_repository(self, **fields: object) -> None:
        """Record repository facts collected during preparation."""
        ...

    def set_quality(self, **fields: object) -> None:
        """Record quality-gate selection facts."""
        ...

    def mark_block(self, reason: str | None) -> None:
        """Mark the invocation as having emitted a block decision."""
        ...

    def disable(self) -> None:
        """Stop writing records for the rest of the invocation."""
        ...

    def excluded_paths(self) -> tuple[ExcludedPath, ...]:
        """Return the log's own entries repository queries must ignore."""
        ...
