"""A contended lock drops one record rather than stalling the hook.

The locks themselves belong to
:class:`~post_turn_quality_stop_hook.decision_log.append.LockedJsonlAppender`,
not to the sink. Each test therefore builds the adapter explicitly and injects
it, which is also how the adapter is shown to be substitutable on its own.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
import typing as typ

import pytest
from decision_log_support import (
    CONTENDED_ATTEMPTS,
    CONTENDED_WAIT_MS,
    TICK_MS,
    _AdvancingClock,
    _FixedClock,
    _log_line,
    _TickingClock,
)

from post_turn_quality_stop_hook import decision_log as log_mod

# The concrete sink and appender are adapters, exported separately from
# the decision log's domain-facing names.
from post_turn_quality_stop_hook.decision_log import adapters as adapters_mod
from post_turn_quality_stop_hook.decision_log import append as append_mod
from post_turn_quality_stop_hook.decision_log import platform_support
from post_turn_quality_stop_hook.decision_log.ownership import DescriptorOwnership
from post_turn_quality_stop_hook.diagnostics import warn

if typ.TYPE_CHECKING:
    from pathlib import Path

# Two records are dropped: the first waits the timeout, the second does not.
BUSY_WARNINGS = 2
LOG_NAME = "hook-2026-11-06.jsonl"
# A held ``flock`` blocks every other descriptor of the same file, even inside
# one process, which is what makes the contention tests possible without a
# child process. Windows byte-range locks are per process, so those tests only
# describe the POSIX adapter.
POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="a held flock conflicts across descriptors; Windows locks are per process",
)


def _sink(
    log_dir: Path,
    clock: log_mod.Clock,
) -> tuple[adapters_mod.MeasuredRecordSink, adapters_mod.LockedJsonlAppender]:
    """Return a measured sink and the adapter it writes through.

    The storage unit, its appender, and the measurement wrapper share one
    telemetry collector, as the composition root wires them, so a test can read
    the storage figures from the wrapper and the lock state from the adapter.
    The wrapper is the ``RecordSink`` boundary; the storage unit itself keeps
    no telemetry.
    """
    stats = log_mod.StorageStats()
    appender = adapters_mod.LockedJsonlAppender(
        clock, ownership=DescriptorOwnership(), on_failure=stats.fail_write
    )
    # The log directory here is a temporary path outside any worktree, but these
    # tests are about locking, so each query is supplied explicitly and answers
    # as Git does there: untracked, and not in a worktree.
    storage = adapters_mod.JsonlRecordSink(
        log_dir,
        clock=clock,
        on_error=warn,
        is_tracked=lambda _candidate: False,
        is_visible=lambda _candidate: False,
        is_tracked_entry=lambda _candidate: False,
        in_worktree=lambda _candidate: False,
        is_ignored=lambda _candidate: False,
        appender=appender,
    )
    sink = adapters_mod.MeasuredRecordSink(
        storage,
        clock=clock,
        stats=stats,
        on_error=warn,
    )
    return sink, appender


class TestWriteLock:
    """A contended lock drops one record rather than stalling the hook."""

    def test_a_signal_handler_re_entry_does_not_wait_for_the_lock(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A finish written from the holding thread is dropped, not deadlocked.

        Catchable signals run their Python handler on the main thread. When
        that handler records an ``interrupted`` finish while the same thread is
        already inside the append, waiting for the lock would wait on itself.
        The adapter refuses the re-entry, so the handler can re-raise promptly.
        """
        log_dir = tmp_path / "log"
        sink, appender = _sink(
            log_dir, _FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0)
        )
        appender._lock.acquire()
        appender._lock_owner = threading.get_ident()
        try:
            started = time.monotonic()
            sink.write({"record_type": "finish"})
            elapsed = time.monotonic() - started
        finally:
            appender._release_process_lock()

        assert elapsed < 1.0, f"re-entry waited {elapsed:.3f}s for its own lock"
        # The file is created before the lock is attempted, so the refused
        # record leaves an empty file rather than nothing at all.
        path = log_dir / LOG_NAME
        assert path.read_text(encoding="utf-8") == ""
        assert "the decision log is busy" in capsys.readouterr().err

    @POSIX_ONLY
    def test_a_signal_during_an_append_writes_through_the_active_descriptor(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The interrupted finish reuses the descriptor whose lock is held.

        A catchable signal runs on the thread that is already inside an
        append, so a second descriptor would conflict with the held ``flock``
        for the whole timeout and lose the record. Once the file is approved
        for this write, the handler writes through the active descriptor
        instead: the record survives, stays under the same lock, and needs no
        wait.
        """
        log_dir = tmp_path / "log"
        sink, appender = _sink(
            log_dir, _FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0)
        )
        # A regression would otherwise wait out the real two-second timeout.
        monkeypatch.setattr(append_mod, "_LOCK_TIMEOUT_SECONDS", 0.0)
        original_write = appender._write
        signalled = False

        def _signal_then_write(descriptor: int, data: bytes) -> bool:
            """Simulate the handler arriving after approval, before the write."""
            nonlocal signalled
            if not signalled:
                signalled = True
                sink.write({"record_type": "finish", "decision": "interrupted"})
            return original_write(descriptor, data)

        monkeypatch.setattr(appender, "_write", _signal_then_write)

        sink.write({"record_type": "progress"})

        captured = capsys.readouterr()
        assert captured.err == "", "the record must not be dropped as busy"
        lines = (log_dir / LOG_NAME).read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["record_type"] for line in lines] == [
            "finish",
            "progress",
        ], "the interrupted record is written through the active descriptor"

    @POSIX_ONLY
    def test_a_signal_before_the_mode_is_tightened_writes_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A descriptor whose mode is still loose is never reused.

        The adapter publishes the descriptor only after tightening it. A signal
        that arrives before that point therefore finds no descriptor it may
        write through: the interrupted record is refused at once, and the file
        keeps only the record whose write was already approved.
        """
        log_dir = tmp_path / "log"
        sink, _appender = _sink(
            log_dir, _FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0)
        )
        monkeypatch.setattr(append_mod, "_LOCK_TIMEOUT_SECONDS", 0.0)
        original_tighten = platform_support.tighten_permissions

        def _signal_then_tighten(descriptor: int) -> None:
            """Simulate the handler arriving before the mode is restricted."""
            sink.write({"record_type": "finish", "decision": "interrupted"})
            original_tighten(descriptor)

        monkeypatch.setattr(
            platform_support, "tighten_permissions", _signal_then_tighten
        )

        sink.write({"record_type": "progress"})

        assert "the decision log is busy" in capsys.readouterr().err
        lines = (log_dir / LOG_NAME).read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["record_type"] for line in lines] == ["progress"], (
            "no record may be written through an unpublished descriptor"
        )

    @POSIX_ONLY
    def test_a_lock_held_by_another_writer_times_out(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A cross-process lock held elsewhere costs one bounded attempt."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / LOG_NAME
        path.touch()
        monkeypatch.setattr(append_mod, "_LOCK_TIMEOUT_SECONDS", 0.0)
        sink, _appender = _sink(
            log_dir, _FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0)
        )

        descriptor = os.open(path, os.O_RDWR | os.O_APPEND)
        try:
            platform_support.lock_exclusive(descriptor)
            sink.write({"record_type": "start"})
        finally:
            os.close(descriptor)

        assert path.read_text(encoding="utf-8") == ""
        assert "the decision log is busy" in capsys.readouterr().err
        stats = sink.storage_stats()
        write = stats["write"]
        assert write["attempts"] == 1
        assert write["failures"] == 1
        assert write["error_categories"] == ["BlockingIOError"]

    def test_a_large_owned_file_is_appended_to(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ownership of a large file is settled without reading it whole.

        A daily file can legitimately grow past any fixed ceiling. Streaming
        verification keeps peak memory bounded while still proving every line
        is the hook's own, so the run appends instead of refusing the file.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        line = _log_line("progress")
        path = log_dir / LOG_NAME
        path.write_text(line * (9 * 1024 * 1024 // len(line)), encoding="utf-8")
        sink, _appender = _sink(
            log_dir, _FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0)
        )

        sink.write({"record_type": "finish"})

        assert "was not written by this hook" not in capsys.readouterr().err
        records = [
            json.loads(record)
            for record in path.read_text(encoding="utf-8").splitlines()
        ]
        assert records[-1]["record_type"] == "finish"

    @POSIX_ONLY
    def test_a_timeout_makes_later_lock_attempts_non_blocking(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """One full wait per run is enough; later records try once.

        Once a record has spent the whole timeout on a lock it did not get,
        the file most likely belongs to another process for the rest of the
        run. Waiting again for every later record would multiply that cost.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / LOG_NAME
        path.touch()
        clock = _AdvancingClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC))
        sink, _appender = _sink(log_dir, clock)

        descriptor = os.open(path, os.O_RDWR | os.O_APPEND)
        try:
            platform_support.lock_exclusive(descriptor)
            sink.write({"record_type": "start"})
            after_first = list(clock.slept)
            sink.write({"record_type": "finish"})
        finally:
            os.close(descriptor)

        assert after_first, "the first attempt must have waited"
        assert clock.slept == after_first, "a later record waited again"
        assert (
            capsys.readouterr().err.count("the decision log is busy") == BUSY_WARNINGS
        )

    def test_a_write_is_timed_with_the_injected_clock(self, tmp_path: Path) -> None:
        """Storage timings come from the supplied clock, not real elapsed time.

        A real write takes microseconds, so a fake clock that charges 250 ms
        per reading makes any hidden ``time.monotonic`` call visible as a zero
        duration.
        """
        clock = _TickingClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.25)
        sink, _appender = _sink(tmp_path / "log", clock)

        sink.write({"record_type": "start"})

        write = sink.storage_stats()["write"]
        storage_ms = typ.cast("int", write["storage_ms"])
        lock_wait_ms = typ.cast("int", write["lock_wait_ms"])
        assert storage_ms >= TICK_MS, f"the injected clock was bypassed: {storage_ms!r}"
        assert lock_wait_ms >= TICK_MS, (
            f"the injected clock was bypassed: {lock_wait_ms!r}"
        )

    def test_a_contended_lock_retries_on_the_injected_clock(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A short retry waits on the supplied sleeper and then succeeds.

        The first two lock attempts find the file held; the third wins. Both
        waits must be requested from the injected clock, which also supplies
        the elapsed lock time recorded for the finish record.
        """
        log_dir = tmp_path / "log"
        clock = _AdvancingClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC))
        sink, _appender = _sink(log_dir, clock)
        real_lock = platform_support.lock_exclusive
        attempts = 0

        def contend(descriptor: int) -> None:
            """Fail the first two attempts, then take the real lock."""
            nonlocal attempts
            attempts += 1
            if attempts <= CONTENDED_ATTEMPTS:
                raise BlockingIOError
            real_lock(descriptor)

        monkeypatch.setattr(platform_support, "lock_exclusive", contend)

        sink.write({"record_type": "start"})

        assert clock.slept == [append_mod._LOCK_RETRY_SECONDS] * 2
        write = sink.storage_stats()["write"]
        assert write["failures"] == 0
        # Two 10 ms waits are the only time the injected clock advanced.
        assert write["lock_wait_ms"] == CONTENDED_WAIT_MS
        assert capsys.readouterr().err == ""
        assert (log_dir / LOG_NAME).read_text(encoding="utf-8")

    def test_a_file_created_before_a_lock_timeout_is_left_in_place(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A created file is left in place when the record was dropped.

        Creation precedes the lock attempt, so a refused lock leaves an empty
        daily file behind. Nothing in the decision reads it: the log is only
        written where Git cannot report it, so the file cannot change a gate.
        """
        log_dir = tmp_path / "log"
        sink, appender = _sink(
            log_dir, _FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0)
        )
        monkeypatch.setattr(appender, "_lock_file", lambda _descriptor: (False, 0))

        sink.write({"record_type": "start"})

        path = log_dir / LOG_NAME
        assert path.exists()
        assert path.read_bytes() == b"", (
            "the record must be dropped rather than written without the lock"
        )
        assert "the decision log is busy" in capsys.readouterr().err


class _FillingClock(_FixedClock):
    """Clock whose first wait stands in for the run that created the file."""

    def __init__(self, path: Path) -> None:
        """Start at a fixed instant and remember the file to fill."""
        super().__init__(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0)
        self._path = path

    def sleep(self, seconds: float) -> None:
        """Fill the file on the first wait, as its creator would."""
        super().sleep(seconds)
        if len(self.slept) == 1:
            self._path.write_text(_log_line(), encoding="utf-8")


class TestCreationWindow:
    """A run that creates the daily file and one that finds it are distinct."""

    def test_a_file_another_run_is_still_creating_is_not_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A zero-length daily file is rechecked before it is called foreign.

        The file is created before it can be locked and written, so a
        concurrent run can hold the lock on a zero-length file that is the
        hook's own. Refusing it would drop every record of that run and warn
        about a file the hook itself had just made.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / LOG_NAME
        path.touch()
        clock = _FillingClock(path)
        sink, _appender = _sink(log_dir, clock)

        sink.write({"record_type": "finish"})

        records = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]
        assert [record["record_type"] for record in records] == [
            "start",
            "finish",
        ], "the waiting run must append after the creator's record"
        assert clock.slept == [append_mod._EMPTY_RETRY_SECONDS], (
            "the file must be rechecked once the creator has had its moment,"
            f" but the clock slept {clock.slept!r}"
        )
        assert capsys.readouterr().err == "", (
            "a file another run was creating must not be reported as foreign"
        )

    def test_an_empty_file_that_stays_empty_is_refused_after_bounded_waits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A genuinely empty file is still refused, after a bounded pause.

        The rechecks exist for a concurrent creator, not to accept a file the
        user made at the daily path. Once the bound is spent the ownership
        check refuses the file exactly as before.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / LOG_NAME
        path.touch()
        clock = _FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0)
        sink, _appender = _sink(log_dir, clock)

        sink.write({"record_type": "start"})

        assert (
            clock.slept
            == [append_mod._EMPTY_RETRY_SECONDS] * append_mod._EMPTY_WAIT_RETRIES
        ), f"the wait must be bounded, but the clock slept {len(clock.slept)} times"
        assert path.read_text(encoding="utf-8") == ""
        assert "not written by this hook" in capsys.readouterr().err


#: Warnings a sink reported, collected so a test can assert on them.
warnings: list[str] = []


def _recorded_warnings(message: str) -> None:
    """Keep a bounded warning the sink reports instead of writing a stream."""
    warnings.append(message)
