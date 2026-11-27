"""Shared observer doubles for the pipeline, quality and logging tests.

Several modules need to assert on the events the pipeline reports. Keeping one
:class:`PipelineObserver` implementation here means a protocol change is a
single edit rather than one per module, and a missed copy cannot surface as a
type error in only one test file.
"""

from __future__ import annotations

import contextlib
import typing as typ

from post_turn_quality_stop_hook.observer import CommandToken
from post_turn_quality_stop_hook.recording import CommandProgress

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def no_warnings(_message: str) -> None:
    """Discard one operator warning a test does not inspect."""


class RecordingObserver:
    """Observer that keeps every event for direct assertions.

    Each protocol method records the structured attributes most tests read
    (``warnings``, ``decisions``, ``telemetry``, ``blocks``, ``selections``) and
    also appends the raw ``(name, payload)`` pair to ``events``, so a logging
    test can assert the order in which events were reported.

    Parameters
    ----------
    records_facts
        Whether the observer keeps the log-only repository facts. A test that
        passes a source which raises unless it is queried sets this ``False``;
        the default keeps the facts, matching a recorder-backed observer.

    """

    def __init__(self, *, records_facts: bool = True) -> None:
        """Start with no recorded events."""
        self._records_facts = records_facts
        self.events: list[tuple[str, object]] = []
        self.warnings: list[str] = []
        self.decisions: list[tuple[str, str, str | None, str | None]] = []
        self.telemetry: list[tuple[str, dict[str, object]]] = []
        self.blocks: list[str | None] = []
        self.selections: list[dict[str, object]] = []
        self.repository_observations: list[dict[str, object]] = []
        self.stages: list[str] = []
        self.started_commands: list[tuple[str, str, tuple[str, ...]]] = []
        self.finished_commands: list[tuple[str | None, int]] = []

    def warning(self, message: str) -> None:
        """Record one operator warning."""
        self.warnings.append(message)
        self.events.append(("warning", message))

    def stage(self, name: str) -> contextlib.AbstractContextManager[None]:
        """Record the stage name and return a no-op context manager."""
        self.stages.append(name)
        self.events.append(("stage", name))
        return contextlib.nullcontext()

    def command_started(self, kind: str, cmd: str, targets: list[str]) -> CommandToken:
        """Record a command start and return a token carrying its kind."""
        copied = list(targets)
        self.started_commands.append((kind, cmd, tuple(copied)))
        self.events.append(("command_started", (kind, cmd, tuple(copied))))
        progress = CommandProgress(kind=kind, cmd=cmd, targets=copied, started=0.0)
        return CommandToken(progress)

    def command_finished(self, token: CommandToken | None, exit_code: int) -> None:
        """Record a command finish against the token's kind."""
        kind = None if token is None else token.progress.kind
        self.finished_commands.append((kind, exit_code))
        self.events.append(("command_finished", (kind, exit_code)))

    def quality_selected(self, fields: cabc.Mapping[str, object]) -> None:
        """Record one quality selection."""
        copied = dict(fields)
        self.selections.append(copied)
        self.events.append(("quality_selected", copied))

    def repository_observed(self, fields: cabc.Mapping[str, object]) -> None:
        """Record one repository observation."""
        copied = dict(fields)
        self.repository_observations.append(copied)
        self.events.append(("repository_observed", copied))

    def records_repository_facts(self) -> bool:
        """Whether this observer keeps the facts it is given."""
        return self._records_facts

    def gate_decided(
        self,
        gate: str,
        outcome: str,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        """Record one gate decision."""
        self.decisions.append((gate, outcome, reason, error))
        self.events.append(("gate_decided", (gate, outcome, reason, error)))

    def blocked(self, reason: str | None) -> None:
        """Record the block marker."""
        self.blocks.append(reason)
        self.events.append(("blocked", reason))

    def gate_telemetry(self, event: str, fields: cabc.Mapping[str, object]) -> None:
        """Record one telemetry event and its fields."""
        copied = dict(fields)
        self.telemetry.append((event, copied))
        self.events.append(("telemetry", (event, copied)))
