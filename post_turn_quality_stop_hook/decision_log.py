"""Durable per-invocation decision log for the stop hook.

The log is an append-only JSON Lines file per UTC day beneath the XDG state
directory. Each hook invocation writes:

- one ``start`` record carrying correlation fields;
- ``progress`` records as stages and commands begin and finish; and
- one ``finish`` record carrying the decision, timings, repository facts,
  gate outcomes, and command results.

A run with a ``start`` record but no ``finish`` record was interrupted. The
log never carries command output, environment contents, or hook payload text,
and a failed write only warns on standard error so the hook decision is never
changed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import os
import signal
import sys
import time
import typing as typ
import uuid
from importlib import metadata
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types

    from post_turn_quality_stop_hook.config import Config

SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 14
MAX_REASON_CHARS = 500
LOG_SUBDIR = Path("post-turn-quality-stop-hook") / "log"
LOG_FILE_PREFIX = "hook-"
LOG_FILE_SUFFIX = ".jsonl"
GATE_ORDER = (
    "quality_checks",
    "uncommitted_changes",
    "unpushed_commits",
    "pr_rebase",
)
TRUTHY_TOKENS = frozenset({"1", "true", "yes", "on"})
FALSY_TOKENS = frozenset({"0", "false", "no", "off"})


def _package_version() -> str:
    """Return the installed package version, or ``unknown`` when unavailable."""
    try:
        return metadata.version("post-turn-quality-stop-hook")
    except metadata.PackageNotFoundError:
        return "unknown"


HOOK_VERSION = _package_version()


@dataclasses.dataclass(slots=True, frozen=True)
class DecisionLogSettings:
    """Resolved decision-log settings.

    Attributes
    ----------
    enabled
        Whether the hook should write a decision log.
    state_home
        XDG state directory that contains the log directory.
    retention_days
        Number of daily files to retain before pruning older files.

    """

    enabled: bool = True
    state_home: Path = dataclasses.field(
        default_factory=lambda: Path.home() / ".local" / "state"
    )
    retention_days: int = DEFAULT_RETENTION_DAYS


def _state_home_from_env() -> Path:
    """Resolve the XDG state directory from the environment."""
    configured = os.environ.get("XDG_STATE_HOME")
    # The XDG specification says to ignore a relative path. Honouring one would
    # put the log inside the repository under test, where the uncommitted
    # changes gate would then see it.
    if configured and Path(configured).is_absolute():
        return Path(configured)
    return Path.home() / ".local" / "state"


def _env_toggle(name: str) -> bool | None:
    """Read a tri-state boolean environment toggle."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    token = raw.strip().lower()
    if token in TRUTHY_TOKENS:
        return True
    if token in FALSY_TOKENS:
        return False
    return None


def parse_retention_days(raw: str | None, default: int) -> int:
    """Parse a non-negative retention period, falling back when invalid.

    Parameters
    ----------
    raw
        Raw environment value.
    default
        Value used when ``raw`` is absent, empty, non-integer, or negative.

    Returns
    -------
    int
        A non-negative retention period in days.

    """
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def settings_from_env(config: Config | None = None) -> DecisionLogSettings:
    """Resolve decision-log settings from configuration and environment.

    Parameters
    ----------
    config
        Loaded hook configuration, or ``None`` when configuration loading
        failed. Environment overrides still apply in that case.

    Returns
    -------
    DecisionLogSettings
        Settings used to construct the per-invocation log.

    """
    enabled = config.log_enabled if config is not None else True
    toggle = _env_toggle("POST_TURN_LOG")
    if toggle is not None:
        enabled = toggle

    retention = (
        config.log_retention_days if config is not None else DEFAULT_RETENTION_DAYS
    )
    retention = parse_retention_days(
        os.environ.get("POST_TURN_LOG_RETENTION_DAYS"), retention
    )
    return DecisionLogSettings(
        enabled=enabled,
        state_home=_state_home_from_env(),
        retention_days=retention,
    )


def log_directory(state_home: Path) -> Path:
    """Return the decision-log directory beneath an XDG state home."""
    return state_home / LOG_SUBDIR


def _log_file_date(path: Path) -> dt.date | None:
    """Parse the date encoded in a decision-log filename."""
    name = path.name
    if not (name.startswith(LOG_FILE_PREFIX) and name.endswith(LOG_FILE_SUFFIX)):
        return None
    encoded = name.removeprefix(LOG_FILE_PREFIX).removesuffix(LOG_FILE_SUFFIX)
    try:
        return dt.date.fromisoformat(encoded)
    except ValueError:
        return None


def prune_old_logs(
    log_dir: Path, retention_days: int, *, today: dt.date | None = None
) -> int:
    """Remove daily log files older than the retention period.

    Parameters
    ----------
    log_dir
        Directory containing ``hook-YYYY-MM-DD.jsonl`` files.
    retention_days
        Number of days to retain. Zero keeps only today's file.
    today
        Current date, injectable for tests. Defaults to the UTC date.

    Returns
    -------
    int
        Number of files removed. Pruning failures are ignored because the log
        must never affect the hook decision.

    """
    if retention_days < 0:
        return 0
    try:
        candidates = list(log_dir.iterdir())
    except OSError:
        return 0

    current = today or dt.datetime.now(dt.UTC).date()
    try:
        cutoff = current - dt.timedelta(days=retention_days)
    except OverflowError:
        # A retention period beyond the calendar range keeps every file; the
        # hook must not abort merely because the configured window is huge.
        return 0
    removed = 0
    for path in candidates:
        file_date = _log_file_date(path)
        if file_date is None or file_date >= cutoff:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def _utc_now() -> dt.datetime:
    """Return the current timezone-aware UTC time."""
    return dt.datetime.now(dt.UTC)


def _elapsed_ms(started: float) -> int:
    """Return whole milliseconds elapsed since a monotonic timestamp."""
    return max(0, int((time.monotonic() - started) * 1000))


def _bounded_reason(value: str | None) -> str | None:
    """Collapse and bound free-text reasons before logging them."""
    if value is None:
        return None
    collapsed = " ".join(value.split())
    if len(collapsed) <= MAX_REASON_CHARS:
        return collapsed
    return collapsed[:MAX_REASON_CHARS]


def _warn(message: str) -> None:
    """Write a warning to standard error without risking the hook decision."""
    try:
        print(f"post-turn-quality-stop-hook: {message}", file=sys.stderr)
    except OSError:
        return


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


@dataclasses.dataclass(slots=True)
class CommandProgress:
    """In-flight command timing used to finish a progress record."""

    kind: str
    cmd: str
    targets: list[str]
    started: float


class DecisionLog:
    """Append structured records for one hook invocation.

    Parameters
    ----------
    settings
        Resolved log settings.
    context
        Facts identifying and locating the invocation.
    now
        Wall-clock source, injectable for tests.

    """

    def __init__(
        self,
        settings: DecisionLogSettings,
        context: InvocationContext,
        *,
        now: cabc.Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        """Write the ``start`` record and prepare to track the run."""
        self._settings = settings
        self._correlation = dict(context.correlation)
        self._start_dir = str(context.start_dir)
        self._base_ref = context.base_ref
        self._now = now
        self._run_id = uuid.uuid4().hex
        self._pid = os.getpid()
        self._started = time.monotonic()
        self._stages: dict[str, int] = {}
        self._gates: dict[str, dict[str, object]] = {}
        self._repository: dict[str, object] = {}
        self._quality: dict[str, object] = {}
        self._commands: list[dict[str, object]] = []
        self._blocked = False
        self._block_reason: str | None = None
        self._finished = False
        self._write_start()
        self._prune()

    @property
    def run_id(self) -> str:
        """Stable identifier shared by every record in this invocation."""
        return self._run_id

    def _common(self) -> dict[str, object]:
        """Return fields shared by every record."""
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self._run_id,
            "timestamp": self._now().isoformat(),
            "pid": self._pid,
            **self._correlation,
        }

    def _log_path(self) -> Path:
        """Return today's log file path."""
        date = self._now().date().isoformat()
        return log_directory(self._settings.state_home) / (
            f"{LOG_FILE_PREFIX}{date}{LOG_FILE_SUFFIX}"
        )

    def _write_record(self, record: dict[str, object]) -> None:
        """Append one record with a single ``O_APPEND`` write."""
        payload = {**self._common(), **record}
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            path = self._log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                data = line.encode("utf-8")
                # Do not retry a short write: a second append could interleave
                # with another hook's record. The warning cannot repair the
                # truncated line, but it makes the failure visible.
                if os.write(descriptor, data) != len(data):
                    _warn("could not write the complete decision log record")
            finally:
                os.close(descriptor)
        except (OSError, UnicodeEncodeError) as exc:
            # A path or command value decoded with ``surrogateescape`` cannot be
            # encoded as UTF-8. The record is lost, but the decision is not.
            _warn(f"could not write the decision log: {exc}")

    def _prune(self) -> None:
        """Prune expired daily files without affecting hook behaviour."""
        prune_old_logs(
            log_directory(self._settings.state_home),
            self._settings.retention_days,
            today=self._now().date(),
        )

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
        started = time.monotonic()
        self._write_progress({"event": "stage_start", "stage": name})
        try:
            yield
        finally:
            duration = _elapsed_ms(started)
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
            started=time.monotonic(),
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

    def command_finish(self, progress: CommandProgress, exit_code: int) -> None:
        """Record a build command's exit code and duration."""
        command: dict[str, object] = {
            "kind": progress.kind,
            "cmd": progress.cmd,
            "targets": progress.targets,
            "exit_code": int(exit_code),
            "duration_ms": _elapsed_ms(progress.started),
        }
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
            "duration_ms": _elapsed_ms(self._started),
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
        self._write_record(record)

    def _handle_signal(self, signum: int, _frame: types.FrameType | None) -> None:
        """Record an interrupted run and re-raise the signal."""
        try:
            self.finish(
                decision="interrupted",
                exit_code=128 + signum,
                signal_name=signal.Signals(signum).name,
            )
        finally:
            # SIGINT must unwind through ``subprocess.run`` so a build child is
            # terminated and reaped. Forcing ``SIG_DFL`` exits immediately and
            # orphans that child, so restore Python's default interrupt handler
            # instead. Other signals keep forcing the default disposition.
            handler = (
                signal.default_int_handler
                if signum == getattr(signal, "SIGINT", None)
                else signal.SIG_DFL
            )
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signum, handler)
            os.kill(os.getpid(), signum)

    @contextlib.contextmanager
    def catch_signals(self) -> cabc.Iterator[None]:
        """Install best-effort handlers for catchable termination signals."""
        installed: list[tuple[int, object]] = []
        for signum in _catchable_signals():
            try:
                previous = signal.getsignal(signum)
                # Respect a disposition the caller deliberately chose: a
                # signal ignored before the hook started must stay ignored. A
                # ``None`` handler comes from native code and cannot be
                # restored by ``signal.signal``, so leave it alone too.
                if previous is None or previous == signal.SIG_IGN:
                    continue
                signal.signal(signum, self._handle_signal)
            except ValueError, OSError:
                continue
            installed.append((signum, previous))
        try:
            yield
        finally:
            for signum, previous in installed:
                try:
                    signal.signal(signum, typ.cast("signal.Handlers", previous))
                except ValueError, OSError:
                    continue


def _catchable_signals() -> tuple[int, ...]:
    """Return termination signals that can carry a finish record."""
    values: list[int] = []
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        value = getattr(signal, name, None)
        if isinstance(value, int):
            values.append(value)
    return tuple(values)


def make_decision_log(
    settings: DecisionLogSettings,
    *,
    correlation: cabc.Mapping[str, object],
    start_dir: Path,
    base_ref: str,
) -> DecisionLog | None:
    """Create a decision log unless logging is disabled."""
    if not settings.enabled:
        return None
    return DecisionLog(
        settings,
        InvocationContext(
            correlation=correlation,
            start_dir=start_dir,
            base_ref=base_ref,
        ),
    )
