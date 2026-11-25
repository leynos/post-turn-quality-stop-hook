"""Domain-shaped observation for the stop-check pipeline.

Gate functions and orchestration report what they decided or ran through this
interface. Only the adapter created at the ``run_stop_checks`` boundary knows
that a decision log exists, so no gate function imports, receives, or holds the
log itself. Tests substitute a narrow observer and assert on the decisions a
gate reported rather than on log calls.
"""

from __future__ import annotations

import contextlib
import dataclasses
import typing as typ

from post_turn_quality_stop_hook.diagnostics import warn

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from post_turn_quality_stop_hook.recording import (
        CommandProgress,
        DecisionRecorder,
        InvocationLog,
    )


@dataclasses.dataclass(slots=True, frozen=True)
class CommandToken:
    """Handle for a command whose start has been recorded.

    The token keeps the recorder's own progress object typed, so finishing the
    command needs neither an opaque ``object`` nor a cast.

    Attributes
    ----------
    progress
        In-flight command timing returned by the recorder.

    """

    progress: CommandProgress


class PipelineObserver(typ.Protocol):
    """Receive the pipeline's decision-relevant events.

    Every method describes a domain event, not a storage operation. An
    implementation backed by the decision log is supplied once, at the
    orchestration boundary; gates see only this protocol.

    This protocol is also the pipeline's single telemetry channel. A fixed
    warning an operator should see goes through :meth:`warning`; nothing in the
    pipeline writes the standard-library logger directly.
    """

    def warning(self, message: str) -> None:
        """Emit one fixed, output-free warning for the operator."""
        ...

    def stage(self, name: str) -> contextlib.AbstractContextManager[None]:
        """Record a stage's start and duration."""
        ...

    def command_started(
        self, kind: str, cmd: str, targets: list[str]
    ) -> CommandToken | None:
        """Record that a blocking command is about to start."""
        ...

    def command_finished(self, token: CommandToken | None, exit_code: int) -> None:
        """Record a command's exit code against the token it returned."""
        ...

    def quality_selected(self, fields: cabc.Mapping[str, object]) -> None:
        """Record the quality targets selected for this run."""
        ...

    def repository_observed(self, fields: cabc.Mapping[str, object]) -> None:
        """Record repository facts observed during preparation."""
        ...

    def records_repository_facts(self) -> bool:
        """Whether this observer keeps the log-only repository facts.

        A no-op observer discards everything it receives, so asking Git for
        facts it would drop would only add work and race exposure to a run
        with logging disabled.
        """
        ...

    def gate_decided(
        self,
        gate: str,
        outcome: str,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        """Record one gate outcome, its short reason, and any Git failure."""
        ...

    def blocked(self, reason: str | None) -> None:
        """Mark the invocation as having emitted a block decision."""
        ...

    def gate_telemetry(self, event: str, fields: cabc.Mapping[str, object]) -> None:
        """Record bounded operator telemetry about one gate decision.

        This carries the values a decision log keeps for a skip or a block:
        the assigned gate name and the fields that explain it. It is a single
        channel so the orchestration boundary never writes a logger itself.
        """
        ...


class NullObserver:
    """Observer for a run whose decision log is disabled.

    Every method accepts any arguments and returns the protocol's neutral
    value. The variadic signatures keep this no-op implementation from
    repeating parameter lists it never reads, while remaining callable through
    :class:`PipelineObserver`; the type checker verifies the return types.

    A warning is the one event this observer does not discard: it is not
    durable telemetry but an operator-facing diagnostic, and a run with logging
    disabled must still report it.
    """

    def warning(  # ruff:ignore[no-self-use]  # instance method is the protocol contract.
        self, message: str
    ) -> None:
        """Write the fixed warning to standard error."""
        warn(message)

    def stage(  # ruff:ignore[no-self-use]  # instance method is the protocol contract.
        self, *_args: object, **_kwargs: object
    ) -> contextlib.AbstractContextManager[None]:
        """Return a no-op stage recorder."""
        return contextlib.nullcontext()

    def command_started(  # ruff:ignore[no-self-use]  # instance method is the protocol contract.
        self, *_args: object, **_kwargs: object
    ) -> CommandToken | None:
        """Record nothing and return no token."""
        return None

    def command_finished(self, *_args: object, **_kwargs: object) -> None:
        """Record nothing."""

    def quality_selected(self, *_args: object, **_kwargs: object) -> None:
        """Record nothing."""

    def repository_observed(self, *_args: object, **_kwargs: object) -> None:
        """Record nothing."""

    def records_repository_facts(  # ruff:ignore[no-self-use]  # instance method is the protocol contract.
        self,
    ) -> bool:
        """Keep no facts, so their collection may be skipped."""
        return False

    def gate_decided(self, *_args: object, **_kwargs: object) -> None:
        """Record nothing."""

    def blocked(self, *_args: object, **_kwargs: object) -> None:
        """Record nothing."""

    def gate_telemetry(self, *_args: object, **_kwargs: object) -> None:
        """Record nothing."""


class RecorderObserver:
    """Observer that writes the pipeline's events through a recorder.

    Parameters
    ----------
    recorder
        Recorder for the run's decision log.

    """

    def __init__(self, recorder: DecisionRecorder) -> None:
        """Store the recorder that receives the events."""
        self._recorder = recorder

    def warning(  # ruff:ignore[no-self-use]  # instance method is the protocol contract.
        self, message: str
    ) -> None:
        """Write the fixed warning to standard error.

        A warning is an operator-facing diagnostic rather than durable
        telemetry, so it does not become a record: the log's schema has no
        free-text field that could carry it.
        """
        warn(message)

    def stage(self, name: str) -> contextlib.AbstractContextManager[None]:
        """Record a stage's start and duration."""
        return self._recorder.stage(name)

    def command_started(
        self, kind: str, cmd: str, targets: list[str]
    ) -> CommandToken | None:
        """Record the command's start and return its typed token."""
        return CommandToken(self._recorder.command_start(kind, cmd, targets))

    def command_finished(self, token: CommandToken | None, exit_code: int) -> None:
        """Record the command's exit code against its token."""
        if token is not None:
            self._recorder.command_finish(token.progress, exit_code)

    def quality_selected(self, fields: cabc.Mapping[str, object]) -> None:
        """Record the selected quality targets."""
        self._recorder.set_quality(**fields)

    def repository_observed(self, fields: cabc.Mapping[str, object]) -> None:
        """Record the observed repository facts."""
        self._recorder.set_repository(**fields)

    def records_repository_facts(self) -> bool:  # ruff:ignore[no-self-use]  # instance method is the protocol contract.
        """Persist the facts through the run's recorder."""
        return True

    def gate_decided(
        self,
        gate: str,
        outcome: str,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        """Record one gate outcome, its reason, and any Git failure."""
        self._recorder.record_gate(gate, outcome, reason, error)

    def blocked(self, reason: str | None) -> None:
        """Mark the emitted block decision."""
        self._recorder.mark_block(reason)

    def gate_telemetry(self, event: str, fields: cabc.Mapping[str, object]) -> None:
        """Record bounded operator telemetry as a progress record."""
        self._recorder.record_telemetry(event, fields)


def observer_for(invocation: InvocationLog) -> PipelineObserver:
    """Return the observer a run should use.

    Parameters
    ----------
    invocation
        Recording configuration for the run.

    Returns
    -------
    PipelineObserver
        A recorder-backed observer, or a no-op observer when logging is off.

    """
    if invocation.recorder is None:
        return NullObserver()
    return RecorderObserver(invocation.recorder)
