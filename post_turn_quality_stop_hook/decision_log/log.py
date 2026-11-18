"""Collect one invocation's records and persist them through a sink."""

from __future__ import annotations

import contextlib
import os
import signal
import typing as typ
import uuid

from post_turn_quality_stop_hook.decision_log.clock import SystemClock, _elapsed_ms
from post_turn_quality_stop_hook.decision_log.constants import (
    GATE_ORDER,
    HOOK_VERSION,
    PRODUCER,
    SCHEMA_VERSION,
)
from post_turn_quality_stop_hook.decision_log.records import (
    DecisionLogDeps,
    _bounded_reason,
    _warn,
)
from post_turn_quality_stop_hook.decision_log.settings import log_directory
from post_turn_quality_stop_hook.decision_log.sink import JsonlRecordSink
from post_turn_quality_stop_hook.git import RepositoryView
from post_turn_quality_stop_hook.recording import CommandProgress

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from post_turn_quality_stop_hook.decision_log.records import InvocationContext
    from post_turn_quality_stop_hook.decision_log.settings import DecisionLogSettings
    from post_turn_quality_stop_hook.git import ExcludedPath


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
        ``state_home``, the system clock, and random identifiers.
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
        self._sink = collaborators.sink or JsonlRecordSink(
            log_directory(settings.state_home),
            clock=self._clock,
            is_tracked=collaborators.is_tracked,
        )
        self._correlation = dict(context.correlation)
        self._start_dir = str(context.start_dir)
        self._base_ref = context.base_ref
        self._run_id = (
            collaborators.run_id_factory()
            if collaborators.run_id_factory is not None
            else uuid.uuid4().hex
        )
        self._pid = os.getpid()
        self._started = self._clock.monotonic() if started is None else started
        # Startup is already complete by the time the log exists, so record it
        # as a finished stage rather than opening one that can never close.
        self._stages: dict[str, int] = {
            "startup": _elapsed_ms(self._started, self._clock.monotonic())
        }
        self._gates: dict[str, dict[str, object]] = {}
        self._repository: dict[str, object] = {}
        self._quality: dict[str, object] = {}
        self._commands: list[dict[str, object]] = []
        self._blocked = False
        self._block_reason: str | None = None
        self._finished = False
        self._disabled = False
        self._write_start()

    @property
    def run_id(self) -> str:
        """Stable identifier shared by every record in this invocation."""
        return self._run_id

    def excluded_paths(self) -> tuple[ExcludedPath, ...] | None:
        """Return the daily files this run wrote to, for repository queries.

        The answer is taken from the sink at each query rather than captured
        once: a run that crosses midnight writes a second daily file, and only
        files the sink actually wrote to may be ignored. A run disabled by a
        later configuration opt-out still excludes the file its start record
        created, because that file exists and would otherwise make the
        worktree dirty.

        ``None`` means the sink could not say which files it wrote. That is
        different from an empty tuple, which says it wrote none: the caller
        can then warn that the exclusion degraded instead of silently counting
        the run's own daily file as a repository change. The exception never
        escapes, because logging must not change the decision.
        """
        try:
            return self._sink.excluded_paths()
        except Exception:  # ruff:ignore[blind-except]  # logging must never change the decision.
            return None

    def repository_view(self) -> RepositoryView:
        """Return repository queries that ignore this run's own log files."""
        return RepositoryView(self.excluded_paths)

    def disable(self) -> None:
        """Stop writing records after a later configuration opt-out.

        The start record may already exist because it is written before the
        authoritative configuration is loaded. A terminal finish record with
        ``decision = "disabled"`` marks the opt-out, so a missing finish keeps
        its documented meaning of an interrupted run. Every later record is
        suppressed, as the opt-out intends.
        """
        self.finish(decision="disabled")
        self._disabled = True

    def set_correlation(self, fields: cabc.Mapping[str, object]) -> None:
        """Add correlation fields once the hook input has been read.

        The ``start`` record is written before standard input is read, so a
        stalled or killed read still leaves evidence that the hook started.
        Correlation that only the payload can supply arrives here, and every
        later record carries it.
        """
        self._correlation.update(fields)
        self._write_progress({"event": "correlation", **fields})

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

        ``RecordSink`` is a protocol, so an injected sink can raise anything.
        The guard belongs here rather than at each caller: ``finish`` runs in
        ``hook.main``'s ``finally`` block and the progress calls run inside the
        pipeline, and an escaping failure could replace a decision or a block
        payload that was already determined.
        """
        if self._disabled:
            return
        try:
            self._sink.write({**self._common(), **record})
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

    @contextlib.contextmanager
    def stage(self, name: str) -> cabc.Iterator[None]:
        """Record a stage's start and duration."""
        started = self._clock.monotonic()
        self._write_progress({"event": "stage_start", "stage": name})
        try:
            yield
        finally:
            duration = _elapsed_ms(started, self._clock.monotonic())
            self._stages[name] = self._stages.get(name, 0) + duration
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
        self._commands.append(command)
        self._write_progress({"event": "command_finish", "command": command})

    def record_gate(self, gate: str, outcome: str, reason: str | None = None) -> None:
        """Record a gate outcome and a short reason."""
        self._gates[gate] = {
            "gate": gate,
            "outcome": outcome,
            "reason": _bounded_reason(reason),
        }

    def set_repository(self, **fields: object) -> None:
        """Record repository facts collected during preparation."""
        self._repository.update(fields)

    def set_quality(self, **fields: object) -> None:
        """Record quality-gate selection facts."""
        self._quality.update(fields)

    def mark_block(self, reason: str | None) -> None:
        """Mark the invocation as having emitted a block decision."""
        self._blocked = True
        self._block_reason = _bounded_reason(reason)

    def finish(
        self,
        *,
        decision: str | None = None,
        exit_code: int = 0,
        error: str | None = None,
        signal_name: str | None = None,
    ) -> None:
        """Write the finish record once, including gates never reached."""
        if self._finished:
            return
        self._finished = True
        resolved = decision or ("block" if self._blocked else "allow")
        gates = [
            self._gates.get(gate)
            or {"gate": gate, "outcome": "not_reached", "reason": None}
            for gate in GATE_ORDER
        ]
        record: dict[str, object] = {
            "record_type": "finish",
            "decision": resolved,
            "exit_code": int(exit_code),
            "error": _bounded_reason(error),
            "duration_ms": _elapsed_ms(self._started, self._clock.monotonic()),
            "stages": dict(self._stages),
            "repository": dict(self._repository),
            "quality": dict(self._quality),
            "gates": gates,
            "commands": list(self._commands),
        }
        if self._block_reason is not None:
            record["block_reason"] = self._block_reason
        if signal_name is not None:
            record["signal"] = signal_name
        storage = self._storage_stats()
        if storage:
            record["storage"] = storage
        self._write_record(record)

    def _storage_stats(self) -> dict[str, object]:
        """Return the sink's bounded storage statistics, when it keeps them.

        ``RecordSink`` is a protocol and a substituted sink need not store
        anything, so the method is optional. A sink that raises must not
        replace the decision either, which is why the call is guarded here.
        """
        getter = getattr(self._sink, "storage_stats", None)
        if getter is None:
            return {}
        try:
            return dict(typ.cast("cabc.Mapping[str, object]", getter()))
        except Exception:  # ruff:ignore[blind-except]  # logging must never change the decision.
            return {}

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
