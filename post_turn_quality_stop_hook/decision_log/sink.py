"""JSON Lines persistence for decision-log records.

The sink only composes the three parts a write needs: a policy that says which
files may be used, a locked appender that performs the one ``O_APPEND`` write,
and a telemetry collector that counts what happened. Each part can be replaced
on its own, so the policy can be tested without a file and the appender without
a policy.
"""

from __future__ import annotations

import json
import typing as typ

from post_turn_quality_stop_hook.decision_log.append import LockedJsonlAppender
from post_turn_quality_stop_hook.decision_log.clock import SystemClock, _elapsed_ms
from post_turn_quality_stop_hook.decision_log.constants import (
    LOG_FILE_PREFIX,
    LOG_FILE_SUFFIX,
    MAX_RECORD_BYTES,
    RECORD_HEADROOM_BYTES,
)
from post_turn_quality_stop_hook.decision_log.policy import WritePolicy
from post_turn_quality_stop_hook.decision_log.records import _warn
from post_turn_quality_stop_hook.decision_log.retention import prune_old_logs
from post_turn_quality_stop_hook.decision_log.stats import StorageStats, error_category
from post_turn_quality_stop_hook.git import is_tracked as git_is_tracked
from post_turn_quality_stop_hook.git import would_appear_as_change

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    from post_turn_quality_stop_hook.decision_log.clock import Clock

# The writer keeps the same margin below the reader's limit that the assembly
# uses for the finish record. A record over the bound would make the whole daily
# file foreign to every later run and to retention, so the one record is dropped
# instead; dropping it cannot change the decision, which is already computed.
_MAX_RECORD_WRITE_BYTES = MAX_RECORD_BYTES - RECORD_HEADROOM_BYTES
_OVERSIZED_CATEGORY = "RecordTooLarge"


class JsonlRecordSink:
    """Append records to one daily JSON Lines file and prune old files.

    Parameters
    ----------
    log_dir
        Directory holding the daily log files.
    clock
        Time source used to choose the daily filename and time the locks.
    on_error
        Callback for retention failures. Receive a fixed message with no path.
    is_tracked
        Git ownership query, passed to the default policy and defaulting to
        :func:`post_turn_quality_stop_hook.git.is_tracked`. The policy refuses
        a daily file Git tracks, or whose status it cannot establish, because a
        write to one would make a clean worktree dirty before the
        uncommitted-changes gate runs and could change the decision.
    is_visible
        Query for whether a worktree could report the daily file as an
        untracked change, passed to the default policy and defaulting to
        :func:`post_turn_quality_stop_hook.git.would_appear_as_change`. Only a
        definite ``False`` permits a write, which is what keeps the log out of
        every decision.
    stats
        Telemetry collector. Defaults to a private one.
    appender
        Locked append adapter. Defaults to one sharing ``clock`` and ``stats``.
    policy
        Ownership and location policy. Defaults to one built from
        ``is_tracked``, ``is_visible`` and ``on_error``.

    """

    def __init__(  # ruff:ignore[too-many-arguments]  # each collaborator is an independent seam the tests substitute.
        self,
        log_dir: Path,
        *,
        clock: Clock | None = None,
        on_error: cabc.Callable[[str], None] = _warn,
        is_tracked: cabc.Callable[[Path], bool | None] = git_is_tracked,
        is_visible: cabc.Callable[[Path], bool | None] = would_appear_as_change,
        stats: StorageStats | None = None,
        appender: LockedJsonlAppender | None = None,
        policy: WritePolicy | None = None,
    ) -> None:
        """Store the log directory and assemble the write path."""
        self._log_dir = log_dir
        self._clock = clock or SystemClock()
        self._on_error = on_error
        self._is_tracked = is_tracked
        self._stats = stats or StorageStats()
        self._appender = appender or LockedJsonlAppender(
            self._clock, stats=self._stats, on_error=on_error
        )
        self._policy = policy or WritePolicy(
            on_error=on_error, is_tracked=is_tracked, is_visible=is_visible
        )

    @property
    def directory(self) -> Path:
        """Directory holding this sink's daily log files."""
        return self._log_dir

    def _log_path(self) -> Path:
        """Return today's log file path."""
        date = self._clock.wall().date().isoformat()
        return self._log_dir / f"{LOG_FILE_PREFIX}{date}{LOG_FILE_SUFFIX}"

    def storage_stats(self) -> dict[str, dict[str, object]]:
        """Return bounded timings and failure categories for the finish record."""
        return self._stats.snapshot()

    def write(self, record: cabc.Mapping[str, object]) -> None:
        """Append one record with a single ``O_APPEND`` write."""
        started = self._clock.monotonic()
        self._stats.start_write()
        lock_wait_ms = 0
        try:
            line = json.dumps(dict(record), ensure_ascii=False, separators=(",", ":"))
            if len(line.encode("utf-8")) > _MAX_RECORD_WRITE_BYTES:
                self._stats.fail_write(_OVERSIZED_CATEGORY)
                self._on_error(
                    f"could not write the decision log ({_OVERSIZED_CATEGORY})"
                )
                return
            path = self._log_path()
            if not self._policy.permits_write(path):
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            result = self._appender.append(path, line, self._policy)
            lock_wait_ms = result.lock_wait_ms
        except (OSError, UnicodeEncodeError) as exc:
            # A path or command value decoded with ``surrogateescape`` cannot be
            # encoded as UTF-8. The record is lost, but the decision is not.
            # The warning stays fixed: exception text would carry the state
            # directory and other personal paths into standard error. Only the
            # allow-listed class name is added.
            category = error_category(exc)
            self._stats.fail_write(category)
            self._on_error(f"could not write the decision log ({category})")
        finally:
            self._stats.add_write(
                storage_ms=_elapsed_ms(started, self._clock.monotonic()),
                lock_wait_ms=lock_wait_ms,
            )

    def prune(self, today: dt.date, retention_days: int) -> None:
        """Prune expired daily files without affecting hook behaviour."""
        if self._policy.refused:
            # The policy refused the log location, so this run may not write.
            # Leave the directory exactly as it was rather than mixing a
            # retention pass into a run that is not allowed to touch its log.
            return
        self._stats.start_prune()
        started = self._clock.monotonic()
        try:
            prune_old_logs(
                self._log_dir,
                retention_days,
                today=today,
                on_error=self._prune_failed,
                is_tracked=self._is_tracked,
            )
        finally:
            self._stats.add_prune(
                storage_ms=_elapsed_ms(started, self._clock.monotonic())
            )

    def _prune_failed(self, message: str) -> None:
        """Count a retention failure and append its bounded category."""
        self._stats.fail_prune("OSError")
        self._on_error(f"{message} (OSError)")
