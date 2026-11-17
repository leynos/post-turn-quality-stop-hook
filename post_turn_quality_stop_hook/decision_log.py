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

Record collection, JSONL persistence, and signal handling are separate. The
pipeline depends on the domain-shaped
:class:`~post_turn_quality_stop_hook.recording.DecisionRecorder` protocol;
:class:`DecisionLog` collects records and delegates storage to a
:class:`RecordSink`, and :class:`SignalGuard` installs termination handlers.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import json
import os
import signal
import sys
import threading
import time
import typing as typ
import uuid
from importlib import metadata
from pathlib import Path

from post_turn_quality_stop_hook.git import ExcludedPath
from post_turn_quality_stop_hook.recording import CommandProgress

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
# Every record names its producer. Retention and the sink use this marker, not
# the filename, to decide whether a file belongs to the hook, so an unrelated
# file that happens to carry a daily name is preserved.
PRODUCER = "post-turn-quality-stop-hook"
# The record types the hook writes. A file whose first record names any other
# type was not written by this hook.
RECORD_TYPES = frozenset({"start", "progress", "finish"})
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


def _warn(message: str) -> None:
    """Write a warning to standard error without risking the hook decision."""
    try:
        print(f"post-turn-quality-stop-hook: {message}", file=sys.stderr)
    except OSError:
        return


# A day's records stay well below this; the bound keeps a misnamed huge file
# from being read in full merely to decide ownership. A file larger than the
# bound cannot be verified, so it is preserved and never appended to.
_OWNERSHIP_READ_LIMIT = 8 * 1024 * 1024
# Appending must never stall the hook. Another run holds the lock only for one
# short write, so a lock that is still held after this long belongs to
# something else, and the record is dropped rather than waited for.
_LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_RETRY_SECONDS = 0.01


def _is_hook_record_line(line: bytes) -> bool:
    """Return whether ``line`` is a record the hook writes.

    A daily filename does not prove ownership: an unrelated untracked file can
    carry the same name, and Git holds no copy of it. Every record the hook
    writes carries the producer marker, one of a closed set of record types,
    and the common fields, so a file that merely happens to be JSON, or that
    uses a schema-like envelope of its own, is preserved.
    """
    try:
        record = json.loads(line)
    except ValueError:
        return False
    if not isinstance(record, dict):
        return False
    version = record.get("schema_version")
    return (
        isinstance(version, int)
        and version >= SCHEMA_VERSION
        and record.get("producer") == PRODUCER
        and record.get("record_type") in RECORD_TYPES
        and isinstance(record.get("run_id"), str)
        and isinstance(record.get("pid"), int)
        and isinstance(record.get("timestamp"), str)
    )


def _is_hook_record_content(content: bytes) -> bool:
    """Return whether ``content`` consists only of records the hook writes.

    Every line is checked, not just the first. A file that begins with a copied
    or stray hook record and continues with unrelated data is not the hook's
    file: appending to it would mix two writers into one line stream, and
    deleting it would destroy data Git cannot restore. Content that does not
    end with a newline is also rejected, because a torn final line cannot be
    verified as a complete record.
    """
    if not content.endswith(b"\n"):
        return False
    return all(_is_hook_record_line(line) for line in content.split(b"\n")[:-1])


def _is_hook_record(path: Path) -> bool:
    """Return whether a file holds only records the hook writes.

    A file that cannot be read, is not a regular file, or is larger than the
    verification bound is preserved. Only a regular file can be a record:
    opening a FIFO that happens to carry a daily name would block the hook
    indefinitely.
    """
    try:
        if not path.is_file():
            return False
        if path.stat().st_size > _OWNERSHIP_READ_LIMIT:
            return False
        content = path.read_bytes()
    except OSError:
        return False
    return _is_hook_record_content(content)


def prune_old_logs(  # ruff:ignore[too-many-arguments]  # directory, window, date, error sink and ownership query are distinct inputs.
    log_dir: Path,
    retention_days: int,
    *,
    today: dt.date | None = None,
    on_error: cabc.Callable[[str], None] | None = None,
    is_tracked: cabc.Callable[[Path], bool | None] | None = None,
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
    on_error
        Callback for scan and deletion failures. It receives a fixed message
        with no filesystem path, because the log must not expose one.
    is_tracked
        Callback reporting whether Git tracks a file, or ``None`` when that is
        unknown. Anything but a confirmed ``False`` is preserved: deleting a
        tracked file would change the worktree, and the decision log must
        never do that.

    Returns
    -------
    int
        Number of files removed. Pruning failures never affect the hook
        decision; they are reported through ``on_error`` so expired files that
        accumulate remain visible.

    """
    cutoff = _retention_cutoff(retention_days, today)
    if cutoff is None:
        return 0
    candidates = _scan_log_dir(log_dir, on_error)
    if candidates is None:
        return 0
    return _remove_expired(candidates, cutoff, on_error, is_tracked)


def _retention_cutoff(retention_days: int, today: dt.date | None) -> dt.date | None:
    """Return the oldest date to keep, or ``None`` when nothing is pruned."""
    if retention_days < 0:
        return None
    current = today or dt.datetime.now(dt.UTC).date()
    try:
        return current - dt.timedelta(days=retention_days)
    except OverflowError:
        # A retention period beyond the calendar range keeps every file; the
        # hook must not abort merely because the configured window is huge.
        return None


def _scan_log_dir(
    log_dir: Path,
    on_error: cabc.Callable[[str], None] | None,
) -> list[Path] | None:
    """List the log directory, reporting a scan failure without its path."""
    try:
        return list(log_dir.iterdir())
    except OSError:
        if on_error is not None:
            on_error("could not scan the decision-log directory")
        return None


def _is_prunable(
    path: Path,
    is_tracked: cabc.Callable[[Path], bool | None] | None,
) -> bool:
    """Return whether retention may delete ``path``.

    A name and an age are not evidence that this file belongs to the hook.
    Requiring the producer's own record shape and a confirmed untracked state
    keeps retention from destroying data Git cannot restore.
    """
    if not _is_hook_record(path):
        return False
    if is_tracked is None:
        return True
    try:
        return is_tracked(path) is False
    except Exception:  # ruff:ignore[blind-except]  # an unknown answer preserves the file.
        return False


def _remove_expired(
    candidates: cabc.Iterable[Path],
    cutoff: dt.date,
    on_error: cabc.Callable[[str], None] | None,
    is_tracked: cabc.Callable[[Path], bool | None] | None,
) -> int:
    """Delete expired daily files the hook owns and Git does not track."""
    removed = 0
    for path in candidates:
        file_date = _log_file_date(path)
        if file_date is None or file_date >= cutoff:
            continue
        if not _is_prunable(path, is_tracked):
            continue
        try:
            path.unlink()
        except OSError:
            if on_error is not None:
                on_error("could not remove an expired decision-log file")
            continue
        removed += 1
    return removed


class Clock(typ.Protocol):
    """Time source used to timestamp and time records."""

    def monotonic(self) -> float:
        """Return a monotonic timestamp in seconds."""
        ...

    def wall(self) -> dt.datetime:
        """Return the current timezone-aware UTC time."""
        ...


class SystemClock:
    """Clock backed by the operating system."""

    @staticmethod
    def monotonic() -> float:
        """Return a monotonic timestamp in seconds."""
        return time.monotonic()

    @staticmethod
    def wall() -> dt.datetime:
        """Return the current timezone-aware UTC time."""
        return dt.datetime.now(dt.UTC)


def _elapsed_ms(started: float, current: float) -> int:
    """Return whole milliseconds between two monotonic timestamps."""
    return max(0, int((current - started) * 1000))


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

    def excluded_paths(self) -> tuple[ExcludedPath, ...]:
        """Return entries repository queries must ignore for this run.

        Only files this sink created may appear here. Deriving the exclusion
        from creation rather than from the daily filename keeps a pre-existing
        untracked file of that name visible to the gates, so enabling the log
        cannot hide a file the gates would otherwise report.
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

    """

    sink: RecordSink | None = None
    clock: Clock | None = None
    run_id_factory: cabc.Callable[[], str] | None = None
    is_tracked: cabc.Callable[[Path], bool | None] | None = None


class JsonlRecordSink:
    """Append records to one daily JSON Lines file and prune old files.

    Parameters
    ----------
    log_dir
        Directory holding the daily log files.
    clock
        Time source used to choose the daily filename.
    on_error
        Callback for retention failures. Receive a fixed message with no path.
    is_tracked
        Git ownership query. A daily file Git tracks is never appended to,
        because that write would make a clean worktree dirty before the
        uncommitted-changes gate runs and could change the decision. The same
        refusal applies when the query cannot answer. Once the sink refuses a
        write, it stays inert for the rest of the run, including retention.

    """

    def __init__(
        self,
        log_dir: Path,
        *,
        clock: Clock | None = None,
        on_error: cabc.Callable[[str], None] = _warn,
        is_tracked: cabc.Callable[[Path], bool | None] | None = None,
    ) -> None:
        """Store the log directory and collaborators."""
        self._log_dir = log_dir
        self._clock = clock or SystemClock()
        self._on_error = on_error
        self._is_tracked = is_tracked
        self._tracking_checked: set[Path] = set()
        self._writable_paths: set[Path] = set()
        self._created_paths: set[Path] = set()
        self._refused = False
        # The in-process lock serialises threads, and its owner identifies a
        # same-thread re-entry from a signal handler.
        self._lock = threading.Lock()
        self._lock_owner: int | None = None

    @property
    def directory(self) -> Path:
        """Directory holding this sink's daily log files."""
        return self._log_dir

    def _log_path(self) -> Path:
        """Return today's log file path."""
        date = self._clock.wall().date().isoformat()
        return self._log_dir / f"{LOG_FILE_PREFIX}{date}{LOG_FILE_SUFFIX}"

    def _may_write(self, path: Path) -> bool:
        """Return whether Git permits appending to ``path``.

        A path that Git tracks, or whose tracking status cannot be established,
        refuses the whole run: the start record is written before the Git
        gates, so a single append to a tracked file could change the decision.
        Ownership of an existing file is settled separately, under the lock in
        :meth:`write`, because only there is the check race-free.
        """
        if path in self._tracking_checked:
            return True
        if self._is_tracked is not None:
            try:
                tracked = self._is_tracked(path)
            except Exception:  # ruff:ignore[blind-except]  # an unknown answer must not risk the decision.
                tracked = None
            if tracked is not False:
                self._refused = True
                self._on_error(
                    "the decision log file is tracked by Git; not writing to it"
                    if tracked
                    else "could not determine whether the decision log file is "
                    "tracked by Git; not writing to it"
                )
                return False
        self._tracking_checked.add(path)
        return True

    def _claim(self, descriptor: int, path: Path, *, created: bool) -> bool:
        """Return whether the locked file may receive this run's records.

        The caller holds an exclusive lock, so no other hook process can be
        midway through its first append. A file this run created is ours by
        definition. An empty file is claimed because it holds no content to
        preserve, and a run killed between creating the file and writing its
        first record would otherwise disable logging for the day. Any other
        file must consist solely of hook records: appending to a foreign file,
        or to one that only begins with a hook record, would modify data the
        hook does not own.
        """
        if created or path in self._writable_paths:
            self._writable_paths.add(path)
            return True
        try:
            size = os.fstat(descriptor).st_size
            if size == 0:
                owned = True
            elif size > _OWNERSHIP_READ_LIMIT:
                owned = False
            else:
                owned = _is_hook_record_content(os.pread(descriptor, size, 0))
        except OSError:  # an unreadable file cannot establish ownership
            owned = False
        if owned:
            self._writable_paths.add(path)
            return True
        self._refused = True
        self._on_error(
            "the decision log file was not written by this hook; not writing to it"
        )
        return False

    def excluded_paths(self) -> tuple[ExcludedPath, ...]:
        """Return the daily files this run created, for repository queries.

        Only a file this run created is listed. A file that already existed,
        even one an earlier invocation of the hook wrote, was already visible
        to the gates before this run started, so appending to it changes
        nothing and hiding it would hide a file logging-disabled runs report.
        The set grows during the run, which also covers a run that crosses
        midnight and writes a second daily file.
        """
        return tuple(
            ExcludedPath(path.parent, path.name) for path in sorted(self._created_paths)
        )

    def _acquire_process_lock(self) -> bool:
        """Take the in-process lock, refusing same-thread re-entry.

        A catchable signal runs its Python handler on the main thread. If the
        handler records an ``interrupted`` finish while that same thread is
        inside :meth:`write`, waiting for the lock would wait on itself for the
        whole timeout. Refusing loses only a record the lock holder is already
        writing, and lets the handler re-raise the signal promptly.
        """
        if self._lock_owner == threading.get_ident():
            return False
        if not self._lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            return False
        self._lock_owner = threading.get_ident()
        return True

    def _release_process_lock(self) -> None:
        """Release the in-process lock taken by :meth:`_acquire_process_lock`."""
        self._lock_owner = None
        self._lock.release()

    @staticmethod
    def _lock_file(descriptor: int) -> bool:
        """Take the cross-process lock, giving up when another run holds it.

        The caller cannot wait indefinitely: the hook must keep running even if
        some other process holds the file. A short bounded retry still absorbs
        the normal case of a concurrent run finishing its single append.
        """
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_LOCK_RETRY_SECONDS)
                continue
            return True

    @staticmethod
    def _open(path: Path) -> tuple[int, bool]:
        """Open the daily file, reporting whether this run created it.

        Creation is detected with ``O_CREAT | O_EXCL`` rather than by size so a
        pre-existing empty file is not mistaken for this run's own; only a file
        this run created may later be excluded from repository queries.
        """
        try:
            descriptor = os.open(
                path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            return os.open(path, os.O_RDWR | os.O_APPEND), False
        return descriptor, True

    def write(self, record: cabc.Mapping[str, object]) -> None:
        """Append one record with a single ``O_APPEND`` write."""
        line = json.dumps(dict(record), ensure_ascii=False, separators=(",", ":"))
        if not self._acquire_process_lock():
            self._on_error("the decision log is busy; not writing the record")
            return
        try:
            self._write_locked(line)
        finally:
            self._release_process_lock()

    def _write_locked(self, line: str) -> None:
        """Append ``line`` while this process holds the in-process lock."""
        try:
            path = self._log_path()
            if self._refused or not self._may_write(path):
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, created = self._open(path)
            try:
                # Serialise the ownership check with the first append. Without
                # the lock, a concurrent run could see this run's file while it
                # is still empty or only partly written and mistake it for a
                # foreign file.
                if not self._lock_file(descriptor):
                    self._on_error("the decision log is busy; not writing the record")
                    return
                if not self._claim(descriptor, path, created=created):
                    return
                if created:
                    # Only a file this run created may be ignored by repository
                    # queries, so a file that already existed stays visible.
                    self._created_paths.add(path)
                # The mode above applies only to a new file. Tighten an existing
                # one too, so a log created by an older version or another
                # process cannot stay readable by other users.
                os.fchmod(descriptor, 0o600)
                data = (line + "\n").encode("utf-8")
                # Do not retry a short write: a second append could interleave
                # with another hook's record. The warning cannot repair the
                # truncated line, but it makes the failure visible.
                if os.write(descriptor, data) != len(data):
                    self._on_error("could not write the complete decision log record")
            finally:
                os.close(descriptor)
        except OSError, UnicodeEncodeError:
            # A path or command value decoded with ``surrogateescape`` cannot be
            # encoded as UTF-8. The record is lost, but the decision is not.
            # The warning stays fixed: exception text would carry the state
            # directory and other personal paths into standard error.
            self._on_error("could not write the decision log")

    def prune(self, today: dt.date, retention_days: int) -> None:
        """Prune expired daily files without affecting hook behaviour."""
        if self._refused:
            # The sink refused to write because a daily file is tracked. Leave
            # the directory exactly as it was rather than mixing a retention
            # pass into a run that is not allowed to touch its log.
            return
        prune_old_logs(
            self._log_dir,
            retention_days,
            today=today,
            on_error=self._on_error,
            is_tracked=self._is_tracked,
        )


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

    def excluded_paths(self) -> tuple[ExcludedPath, ...]:
        """Return the daily files this run wrote, for repository queries.

        The answer is taken from the sink at each query rather than captured
        once: a run that crosses midnight creates a second daily file, and only
        files the sink actually created may be ignored. A run disabled by a
        later configuration opt-out still excludes the file its start record
        created, because that file exists and would otherwise make the
        worktree dirty. A sink failure
        excludes nothing rather than escaping: an unserialised exclusion is
        noise in a gate, while an escaping exception would change the
        decision.
        """
        try:
            return self._sink.excluded_paths()
        except Exception:  # ruff:ignore[blind-except]  # logging must never change the decision.
            return ()

    def disable(self) -> None:
        """Stop writing records after a later configuration opt-out.

        The start record may already exist because it is written before the
        authoritative configuration is loaded. Suppressing every later record
        keeps the opt-out as close to its intent as the append-only format
        allows.
        """
        self._disabled = True
        self._finished = True

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
        self._write_record(record)

    def record_signal(self, signum: int) -> None:
        """Record an interrupted run before the signal is re-raised."""
        self.finish(
            decision="interrupted",
            exit_code=128 + signum,
            signal_name=signal.Signals(signum).name,
        )


class SignalGuard:
    """Install best-effort handlers for catchable termination signals.

    Parameters
    ----------
    on_signal
        Callback invoked with the signal number before the signal is
        re-raised. It must not raise.

    """

    def __init__(self, on_signal: cabc.Callable[[int], None]) -> None:
        """Store the callback invoked before a caught signal is re-raised."""
        self._on_signal = on_signal
        self._installed: list[tuple[int, object]] = []

    def _handle(self, signum: int, _frame: types.FrameType | None) -> None:
        """Record the interruption and re-raise the signal."""
        try:
            self._on_signal(signum)
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

    def __enter__(self) -> SignalGuard:
        """Install handlers, preserving dispositions the caller chose."""
        for signum in _catchable_signals():
            try:
                previous = signal.getsignal(signum)
                # Respect a disposition the caller deliberately chose: a
                # signal ignored before the hook started must stay ignored. A
                # ``None`` handler comes from native code and cannot be
                # restored by ``signal.signal``, so leave it alone too.
                if previous is None or previous == signal.SIG_IGN:
                    continue
                signal.signal(signum, self._handle)
            except ValueError, OSError:
                continue
            self._installed.append((signum, previous))
        return self

    def __exit__(self, *_exc: object) -> None:
        """Restore the handlers that were previously installed."""
        for signum, previous in self._installed:
            try:
                signal.signal(signum, typ.cast("signal.Handlers", previous))
            except ValueError, OSError:
                continue
        self._installed.clear()


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
    context: InvocationContext,
    *,
    deps: DecisionLogDeps | None = None,
    started: float | None = None,
) -> DecisionLog | None:
    """Create a decision log unless logging is disabled."""
    if not settings.enabled:
        return None
    return DecisionLog(settings, context, deps=deps, started=started)
