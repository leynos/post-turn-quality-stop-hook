"""Composition of one hook invocation's decision-log lifecycle.

The CLI entry point owns argument and configuration handling. This module owns
everything the durable log needs around that work:

- the log is created before standard input is read, so a stalled read still
  leaves a start record;
- the signal guard is installed for the whole run and stays installed while the
  terminal record is written;
- correlation from the hook payload is attached once it is known;
- exactly one terminal record is written, carrying the exit code on the
  ordinary path or the exception this invocation raised.

The exception is passed explicitly by the guard's ``__exit__``, so a caller
that invokes the hook inside its own ``except`` block does not make a handled
error look like this invocation's failure.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import typing as typ
import uuid
from pathlib import Path

from post_turn_quality_stop_hook.config import Config, ConfigError
from post_turn_quality_stop_hook.decision_log import (
    DecisionLog,
    InvocationContext,
    StorageStats,
    make_decision_log,
    settings_from_env,
)
from post_turn_quality_stop_hook.decision_log.adapters import (
    SignalGuard,
    default_jsonl_sink_for,
    signal_name,
    terminal_commit,
)
from post_turn_quality_stop_hook.decision_log.clock import SystemClock
from post_turn_quality_stop_hook.diagnostics import warn

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dataclasses.dataclass(slots=True, frozen=True)
class ProcessSnapshot:
    """The process environment and home directory, read once per invocation.

    Settings resolution happens twice: once before the configuration is known
    and once after it is authoritative. Both reads must describe the same
    process, so the composition edge snapshots ``os.environ`` and
    ``Path.home()`` once and passes this value to both calls.
    """

    environ: cabc.Mapping[str, str]
    home: Path

    @classmethod
    def read(cls) -> ProcessSnapshot:
        """Read the process environment and home exactly once.

        A home directory that cannot be resolved leaves the snapshot's home
        relative, which the write policy refuses as a log location. Logging
        then stays inert rather than changing the hook decision, matching the
        guarantee that a logging failure never affects the run.
        """
        try:
            home = Path.home()
        except OSError, RuntimeError:
            home = Path()
        return cls(environ=dict(os.environ), home=home)


#: Hook-input fields copied into every record of the run.
_CORRELATION_FIELDS = (
    "session_id",
    "transcript_path",
    "hook_event_name",
    "stop_hook_active",
)


def start_decision_log(
    context: InvocationContext,
    started: float,
    *,
    process: ProcessSnapshot,
) -> DecisionLog | None:
    """Create the decision log used by the rest of the invocation.

    The log is created before standard input, repository discovery, and the
    configuration load, so a stall or a timeout in any of them leaves a start
    record and an installed signal handler. Only environment settings are
    known this early; the configuration's opt-out and retention window are
    applied once it has been read.

    This is the composition root, so it is also where the run's sink is built:
    :func:`default_jsonl_sink_for` binds the real Git queries that keep the log
    out of the decision. A tracked daily file is never touched, and a location
    the uncommitted-changes gate could report is refused, so enabling the log
    cannot change what that gate answers.

    The sink and the log share one :class:`StorageStats` collector, so the
    finish record's ``storage`` progress describes the writes the sink actually
    attempted. The process-backed collaborators the domain refuses to default
    are bound here: the system clock, a UUID run identifier, the process id,
    the platform signal-name table, and the signal mask around a terminal
    commit.
    """
    try:
        # The process environment and home are supplied here rather than read
        # inside the resolver, so settings resolution has no hidden global
        # dependency and a test can supply both without patching the process.
        log_settings = settings_from_env(environ=process.environ, home=process.home)
        storage = StorageStats()
        return make_decision_log(
            log_settings,
            context,
            sink=default_jsonl_sink_for(
                log_settings,
                on_error=warn,
                stats=storage,
                environ=process.environ,
            ),
            clock=SystemClock(),
            run_id_factory=lambda: uuid.uuid4().hex,
            pid=os.getpid(),
            signal_name=signal_name,
            terminal_commit=terminal_commit,
            storage=storage,
            started=started,
            on_error=warn,
        )
    except Exception:  # ruff:ignore[blind-except]  # logging must never change the decision.
        warn("could not create the decision log")
        return None


def apply_config_settings(
    decision_log: DecisionLog | None,
    config: Config | ConfigError,
    *,
    process: ProcessSnapshot,
) -> None:
    """Apply the log settings only the authoritative configuration supplies.

    The log is created before this read using environment settings alone, so
    the configured retention window and opt-out are resolved here. Pruning now
    keeps a repository's larger window from deleting files that the startup
    default would not have kept.
    """
    if decision_log is None:
        return
    log_settings = settings_from_env(
        config if isinstance(config, Config) else None,
        environ=process.environ,
        home=process.home,
    )
    if log_settings.enabled:
        decision_log.retention.apply(log_settings.retention_days)
    else:
        # The configuration opted out after the start record was written.
        # Suppress every later record.
        decision_log.disable()


class DecisionLogRun:
    """Own one invocation's decision log and signal guard.

    A caller enters the run once, uses :meth:`stage` and
    :meth:`set_correlation` while working, and calls :meth:`finish` with the
    exit code on the ordinary path. An exception that propagates out of the
    ``with`` body is recorded here and re-raised: the exception object comes
    from the context manager, not from ``sys.exc_info``, so an outer handled
    exception cannot be mistaken for this invocation's failure.
    """

    def __init__(
        self, decision_log: DecisionLog | None, *, process: ProcessSnapshot
    ) -> None:
        """Store the log the run finalises and its process snapshot."""
        self._log = decision_log
        self._process = process
        self._guard: SignalGuard | None = None
        self._exit_code = 0
        self._finished = False

    @property
    def recorder(self) -> DecisionLog | None:
        """The log a caller passes to the pipeline's invocation seam."""
        return self._log

    def __enter__(self) -> DecisionLogRun:
        """Install the signal guard for the duration of the run."""
        if self._log is not None:
            self._guard = SignalGuard(self._log.record_signal)
            self._guard.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        """Write the terminal record before restoring the signal handlers."""
        try:
            self._finish(exc)
        finally:
            if self._guard is not None:
                self._guard.__exit__(exc_type, exc, traceback)

    def _finish(self, exc: BaseException | None) -> None:
        """Write the run's single terminal record."""
        if self._log is None or self._finished:
            return
        self._finished = True
        if exc is not None:
            # A message can carry a path, an environment value, or command
            # output, so the log records only the class name.
            self._log.finish(
                decision="error",
                exit_code=1,
                error=type(exc).__name__,
            )
            return
        self._log.finish(exit_code=self._exit_code)

    def stage(self, name: str) -> contextlib.AbstractContextManager[None]:
        """Return a stage recorder, or a no-op when logging is disabled."""
        if self._log is None:
            return contextlib.nullcontext()
        return self._log.stage(name)

    def set_correlation(self, hook_input: cabc.Mapping[str, object]) -> None:
        """Attach the correlation fields the parsed payload supplied."""
        if self._log is not None:
            self._log.set_correlation({
                field: hook_input.get(field) for field in _CORRELATION_FIELDS
            })

    def apply_config(self, config: Config | ConfigError) -> None:
        """Apply the configured log opt-out or retention window."""
        apply_config_settings(self._log, config, process=self._process)

    def mark_block(self, reason: str) -> None:
        """Record that the invocation emitted a block decision."""
        if self._log is not None:
            self._log.mark_block(reason)

    def finish(self, exit_code: int) -> None:
        """Record the exit code the ordinary path is about to return."""
        self._exit_code = exit_code


@contextlib.contextmanager
def decision_log_run(
    *,
    start_dir: Path,
    base_ref: str,
    started: float,
    process: ProcessSnapshot | None = None,
) -> typ.Iterator[DecisionLogRun]:
    """Yield the decision-log lifecycle for one invocation.

    ``process`` is the environment and home snapshot the caller already read.
    Supplying it lets ``main`` read the process environment exactly once and
    share it with the rest of the invocation; omitting it reads a fresh
    snapshot, so a direct caller needs no composition.
    """
    snapshot = process or ProcessSnapshot.read()
    log = start_decision_log(
        InvocationContext(correlation={}, start_dir=start_dir, base_ref=base_ref),
        started,
        process=snapshot,
    )
    with DecisionLogRun(log, process=snapshot) as run:
        yield run
