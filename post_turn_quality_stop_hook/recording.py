"""Domain-shaped recording interface for hook progress and decisions.

The pipeline and gate policy depend on this protocol rather than on the JSONL
persistence adapter. That keeps record collection independent of where and how
records are stored, and lets tests substitute a narrow recorder.
"""

from __future__ import annotations

import dataclasses
import typing as typ

from post_turn_quality_stop_hook.git import RepositoryView

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import contextlib


@dataclasses.dataclass(slots=True, frozen=True)
class InvocationLog:
    """Recording for one run and the repository view its queries use.

    Attributes
    ----------
    recorder
        Recorder for progress and decisions, or ``None`` when logging is off.

    """

    recorder: DecisionRecorder | None = None

    def repository_view(self) -> RepositoryView:
        """Return repository queries that ignore this run's own log files.

        The view asks the recorder for the current exclusions each time it
        queries, so a run that crosses midnight excludes the second daily file
        too, and a run that wrote nothing excludes nothing.
        """
        if self.recorder is None:
            return RepositoryView()
        return self.recorder.repository_view()


@dataclasses.dataclass(slots=True)
class CommandProgress:
    """In-flight command timing used to finish a progress record."""

    kind: str
    cmd: str
    targets: list[str]
    started: float


@dataclasses.dataclass(slots=True)
class GateOutcomes:
    """Gate outcomes collected as gates decide, recorded by the orchestrator.

    A gate reports what it decided without knowing that a decision log exists.
    The orchestrator drains this collection in one place, so recording stays
    independent of how many gates ran and of which branch each one returned
    from.
    """

    _pending: list[tuple[str, str, str | None]] = dataclasses.field(
        default_factory=list
    )

    def note(self, gate: str, outcome: str, reason: str | None = None) -> None:
        """Collect one gate outcome for later recording."""
        self._pending.append((gate, outcome, reason))

    def record_into(self, recorder: DecisionRecorder | None) -> None:
        """Drain the collected outcomes into ``recorder``, when there is one."""
        pending, self._pending = self._pending, []
        if recorder is None:
            return
        for gate, outcome, reason in pending:
            recorder.record_gate(gate, outcome, reason)


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

    def repository_view(self) -> RepositoryView:
        """Return repository queries that ignore this run's own log files."""
        ...
