"""Orchestrate one invocation's decision log.

The log writes each record through the sink, applies retention once the
configured window is known, and keeps the stage, gate, and command facts in a
:class:`~post_turn_quality_stop_hook.decision_log.assembly.RunRecord`. It owns
no record shape of its own and no storage policy: both live in their own units,
so either can change without touching the other.
"""

from __future__ import annotations

import contextlib
import os
import signal
import typing as typ
import uuid

from post_turn_quality_stop_hook.decision_log.assembly import RunRecord, _bounded
from post_turn_quality_stop_hook.decision_log.clock import SystemClock, _elapsed_ms
from post_turn_quality_stop_hook.decision_log.constants import (
    HOOK_VERSION,
    PRODUCER,
    SCHEMA_VERSION,
)
from post_turn_quality_stop_hook.decision_log.records import (
    DecisionLogDeps,
    _bounded_value,
    _warn,
)
from post_turn_quality_stop_hook.decision_log.settings import log_directory
from post_turn_quality_stop_hook.decision_log.sink import JsonlRecordSink
from post_turn_quality_stop_hook.decision_log.stats import StorageStats
from post_turn_quality_stop_hook.recording import CommandProgress

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from post_turn_quality_stop_hook.decision_log.records import InvocationContext
    from post_turn_quality_stop_hook.decision_log.settings import DecisionLogSettings


class DecisionLog:
    """Collect structured records for one hook invocation.

    Parameters
    ----------
    settings
        Resolved log settings.
    context
        Facts identifying and locating the invocation.
    deps
        Substitutable collaborators. Defaults to a JSONL sink beneath
        ``state_home``, the system clock, a private storage collector, and
        random identifiers.
    started
        Monotonic timestamp at which the hook process began its work.

    """

    def __init__(
        self,
        settings: DecisionLogSettings,
        context: InvocationContext,
        *,
        deps: DecisionLogDeps | None = None,
        started: float | None = None,
    ) -> None:
        """Write the ``start`` record and prepare to track the run.

        ``started`` is the monotonic timestamp at which the hook process began
        its work. Passing it makes the finish record's total duration include
        startup, which happens before the log exists.
        """
        collaborators = deps or DecisionLogDeps()
        self._clock = collaborators.clock or SystemClock()
        # The collector is an explicit dependency rather than something read
        # back from the sink: a substituted sink need not keep telemetry, and
        # the log must still be able to describe the storage it asked for.
        self._storage = collaborators.storage or StorageStats()
        self._sink = collaborators.sink or JsonlRecordSink(
            log_directory(settings.state_home),
            clock=self._clock,
            is_tracked=collaborators.is_tracked,
            is_visible=collaborators.is_visible,
            stats=self._storage,
        )
        self._correlation = {
            key: _bounded_value(value) for key, value in context.correlation.items()
        }
        self._run = RunRecord(
            self._clock,
            started=self._clock.monotonic() if started is None else started,
        )
        self._start_dir = str(context.start_dir)
        self._base_ref = context.base_ref
        self._run_id = (
            collaborators.run_id_factory()
            if collaborators.run_id_factory is not None
            else uuid.uuid4().hex
        )
        self._pid = os.getpid()
        self._disabled = False
        # Delivery, not assembly, is what happens once. A signal that arrives
        # after the finish record is built but before it is written calls
        # ``record_signal``; leaving delivery open lets that handler write the
        # interrupted record instead of finding the run already finished.
        self._delivered = False
        self._write_start()

    @property
    def run_id(self) -> str:
        """Stable identifier shared by every record in this invocation."""
        return self._run_id

    def disable(self) -> None:
        """Stop writing records after a later configuration opt-out.

        The start record may already exist because it is written before the
        authoritative configuration is loaded. A terminal finish record with
        ``decision = "disabled"`` marks the opt-out, so a missing finish keeps
        its documented meaning of an interrupted run. Every later record is
        suppressed, as the opt-out intends, including storage telemetry: an
        opt-out writes its terminal record and nothing else.
        """
        if self._disabled or self._delivered:
            return
        self._write_record(self._run.finish(decision="disabled"))
        self._delivered = True
        self._disabled = True

    def set_correlation(self, fields: cabc.Mapping[str, object]) -> None:
        """Add correlation fields once the hook input has been read.

        The ``start`` record is written before standard input is read, so a
        stalled or killed read still leaves evidence that the hook started.
        Correlation that only the payload can supply arrives here, and every
        later record carries it. Each value is bounded first, so an outsized
        payload identifier cannot grow every later record past the ownership
        reader's per-line limit.
        """
        bounded = {key: _bounded_value(value) for key, value in fields.items()}
        self._correlation.update(bounded)
        self._write_progress({"event": "correlation", **bounded})

    def _common(self) -> dict[str, object]:
        """Return fields shared by every record."""
        return {
            "schema_version": SCHEMA_VERSION,
            "producer": PRODUCER,
            "run_id": self._run_id,
            "timestamp": self._clock.wall().isoformat(),
            "pid": self._pid,
            **self._correlation,
        }

    def _write_record(self, record: dict[str, object]) -> None:
        """Append one record with a single ``O_APPEND`` write.

        The size bound applies after the common fields are merged. A finish
        record that fits on its own can exceed the writer's limit once the
        envelope is added, and the sink would then drop the very record that
        carries the decision. Bounding the complete record keeps the optional
        sections, rather than the decision, as what is lost.

        ``RecordSink`` is a protocol, so an injected sink can raise anything.
        The guard belongs here rather than at each caller: ``finish`` runs in
        ``hook.main``'s ``finally`` block and the progress calls run inside the
        pipeline, and an escaping failure could replace a decision or a block
        payload that was already determined.
        """
        if self._disabled:
            return
        try:
            self._sink.write(_bounded({**self._common(), **record}))
        except Exception:  # ruff:ignore[blind-except]  # logging must never change the decision.
            _warn("could not record a decision-log entry")

    def apply_retention(self, retention_days: int) -> None:
        """Prune expired daily files once the retention window is resolved.

        The log is created before configuration is read, so it cannot know the
        configured window in its constructor. Pruning here keeps a repository's
        larger window from deleting files that the startup default would not
        have kept.
        """
        if self._disabled:
            return
        try:
            self._sink.prune(self._clock.wall().date(), retention_days)
        except Exception:  # ruff:ignore[blind-except]  # logging must never change the decision.
            _warn("could not prune the decision log")

    def _write_start(self) -> None:
        """Write the run's start record."""
        self._write_record({
            "record_type": "start",
            "hook_version": HOOK_VERSION,
            "start_dir": self._start_dir,
            "base_ref": self._base_ref,
        })

    def _write_progress(self, fields: dict[str, object]) -> None:
        """Write one progress record."""
        self._write_record({"record_type": "progress", **fields})

    def _write_storage(self) -> None:
        """Write storage telemetry as its own record.

        Telemetry is not part of the finish record: the finish record carries
        the decision, and an operator reading storage behaviour should not have
        to know which terminal record happens to include it. The record is
        omitted when nothing attempted storage, so a log with no writer does
        not claim to have measured one.
        """
        if not self._storage.attempts:
            return
        operations = list(self._storage.snapshot().values())
        self._write_progress({"event": "storage", "operations": operations})

    @contextlib.contextmanager
    def stage(self, name: str) -> cabc.Iterator[None]:
        """Record a stage's start and duration."""
        started = self._clock.monotonic()
        self._write_progress({"event": "stage_start", "stage": name})
        try:
            yield
        finally:
            duration = _elapsed_ms(started, self._clock.monotonic())
            self._run.stage_finished(name, duration)
            self._write_progress({
                "event": "stage_finish",
                "stage": name,
                "duration_ms": duration,
            })

    def command_start(self, kind: str, cmd: str, targets: list[str]) -> CommandProgress:
        """Record that a build command is about to start."""
        progress = CommandProgress(
            kind=kind,
            cmd=cmd,
            targets=list(targets),
            started=self._clock.monotonic(),
        )
        self._write_progress({
            "event": "command_start",
            "command": {
                "kind": progress.kind,
                "cmd": progress.cmd,
                "targets": progress.targets,
            },
        })
        return progress

    def command_finish(
        self,
        progress: CommandProgress,
        exit_code: int,
        detail: cabc.Mapping[str, object] | None = None,
    ) -> None:
        """Record a build command's exit code and duration."""
        command: dict[str, object] = {
            "kind": progress.kind,
            "cmd": progress.cmd,
            "targets": progress.targets,
            "exit_code": int(exit_code),
            "duration_ms": _elapsed_ms(progress.started, self._clock.monotonic()),
        }
        if detail is not None:
            command.update(detail)
        self._run.add_command(command)
        self._write_progress({"event": "command_finish", "command": command})

    def record_gate(self, gate: str, outcome: str, reason: str | None = None) -> None:
        """Record a gate outcome and a short reason."""
        self._run.gate(gate, outcome, reason)

    def set_repository(self, **fields: object) -> None:
        """Record repository facts collected during preparation."""
        self._run.set_repository(fields)

    def set_quality(self, **fields: object) -> None:
        """Record quality-gate selection facts."""
        self._run.set_quality(fields)

    def mark_block(self, reason: str | None) -> None:
        """Mark the invocation as having emitted a block decision."""
        self._run.block(reason)

    def finish(
        self,
        *,
        decision: str | None = None,
        exit_code: int = 0,
        error: str | None = None,
        signal_name: str | None = None,
    ) -> None:
        """Write storage telemetry and the finish record once.

        A terminal record is delivered at most once. The record itself is
        assembled first, then delivered; a signal that arrives during that
        delivery re-enters here and writes an ``interrupted`` record instead of
        returning because the run was already marked finished.
        """
        if self._delivered or self._disabled:
            return
        record = self._run.finish(
            decision=decision,
            exit_code=exit_code,
            error=error,
            signal_name=signal_name,
        )
        self._write_storage()
        if self._delivered:
            # A signal delivered the interrupted record while the telemetry
            # was being written; the record assembled before it is stale.
            return
        self._write_record(record)
        self._delivered = True

    def record_signal(self, signum: int) -> None:
        """Record an interrupted run before the signal is re-raised."""
        self.finish(
            decision="interrupted",
            exit_code=128 + signum,
            signal_name=signal.Signals(signum).name,
        )


def make_decision_log(
    settings: DecisionLogSettings,
    context: InvocationContext,
    *,
    deps: DecisionLogDeps | None = None,
    started: float | None = None,
) -> DecisionLog | None:
    """Create a decision log unless logging is disabled."""
    if not settings.enabled:
        return None
    return DecisionLog(settings, context, deps=deps, started=started)
