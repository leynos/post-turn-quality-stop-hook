"""A contended lock drops one record rather than stalling the hook."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
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

if typ.TYPE_CHECKING:
    from pathlib import Path

# Two records are dropped: the first waits the timeout, the second does not.
BUSY_WARNINGS = 2


class TestWriteLock:
    """A contended lock drops one record rather than stalling the hook."""

    def _sink(self, log_dir: Path) -> log_mod.JsonlRecordSink:
        """Return a sink writing to ``log_dir`` on a fixed day."""
        return log_mod.JsonlRecordSink(
            log_dir,
            clock=_FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0),
            is_tracked=lambda _candidate: False,
        )

    def test_a_signal_handler_re_entry_does_not_wait_for_the_lock(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A finish written from the holding thread is dropped, not deadlocked.

        Catchable signals run their Python handler on the main thread. When
        that handler records an ``interrupted`` finish while the same thread is
        already inside ``write``, waiting for the lock would wait on itself.
        """
        log_dir = tmp_path / "log"
        sink = self._sink(log_dir)
        sink._lock.acquire()
        sink._lock_owner = threading.get_ident()
        try:
            started = time.monotonic()
            sink.write({"record_type": "finish"})
            elapsed = time.monotonic() - started
        finally:
            sink._lock.release()

        assert elapsed < 1.0, f"re-entry waited {elapsed:.3f}s for its own lock"
        assert not (log_dir / "hook-2026-11-06.jsonl").exists()
        assert "the decision log is busy" in capsys.readouterr().err

    def test_a_lock_held_by_another_writer_times_out(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A cross-process lock held elsewhere costs one bounded attempt."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / "hook-2026-11-06.jsonl"
        path.touch()
        monkeypatch.setattr(log_mod.sink, "_LOCK_TIMEOUT_SECONDS", 0.0)
        sink = self._sink(log_dir)

        descriptor = os.open(path, os.O_RDWR | os.O_APPEND)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            sink.write({"record_type": "start"})
        finally:
            os.close(descriptor)

        assert path.read_text(encoding="utf-8") == ""
        assert sink.excluded_paths() == ()
        assert "the decision log is busy" in capsys.readouterr().err
        stats = sink.storage_stats()
        write = typ.cast("dict[str, object]", stats["write"])
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
        path = log_dir / "hook-2026-11-06.jsonl"
        path.write_text(line * (9 * 1024 * 1024 // len(line)), encoding="utf-8")
        sink = self._sink(log_dir)

        sink.write({"record_type": "finish"})

        assert "was not written by this hook" not in capsys.readouterr().err
        records = [
            json.loads(record)
            for record in path.read_text(encoding="utf-8").splitlines()
        ]
        assert records[-1]["record_type"] == "finish"
        assert sink.excluded_paths() != ()

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
        path = log_dir / "hook-2026-11-06.jsonl"
        path.touch()
        clock = _AdvancingClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC))
        sink = log_mod.JsonlRecordSink(
            log_dir, clock=clock, is_tracked=lambda _candidate: False
        )

        descriptor = os.open(path, os.O_RDWR | os.O_APPEND)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
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
        sink = log_mod.JsonlRecordSink(
            tmp_path / "log", clock=clock, is_tracked=lambda _candidate: False
        )

        sink.write({"record_type": "start"})

        write = typ.cast("dict[str, object]", sink.storage_stats()["write"])
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
        sink = log_mod.JsonlRecordSink(
            log_dir, clock=clock, is_tracked=lambda _candidate: False
        )
        real_flock = log_mod.sink.platform_support.fcntl.flock
        attempts = 0

        def contend(descriptor: int, operation: int) -> None:
            """Fail the first two attempts, then take the real lock."""
            nonlocal attempts
            attempts += 1
            if attempts <= CONTENDED_ATTEMPTS:
                raise BlockingIOError
            real_flock(descriptor, operation)

        monkeypatch.setattr(log_mod.sink.platform_support.fcntl, "flock", contend)

        sink.write({"record_type": "start"})

        assert clock.slept == [log_mod.sink._LOCK_RETRY_SECONDS] * 2
        write = typ.cast("dict[str, object]", sink.storage_stats()["write"])
        assert write["failures"] == 0
        # Two 10 ms waits are the only time the injected clock advanced.
        assert write["lock_wait_ms"] == CONTENDED_WAIT_MS
        assert capsys.readouterr().err == ""
        assert (log_dir / "hook-2026-11-06.jsonl").read_text(encoding="utf-8")

    def test_a_file_created_before_a_lock_timeout_stays_excluded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A created file stays hidden even when the record was dropped.

        Creation precedes the lock attempt, so a refused lock would otherwise
        leave an empty daily file behind as a new untracked change. That file
        is repository content the run made, so the run must keep excluding it.
        """
        log_dir = tmp_path / "log"
        sink = self._sink(log_dir)
        monkeypatch.setattr(sink, "_lock_file", lambda _descriptor: False)

        sink.write({"record_type": "start"})

        path = log_dir / "hook-2026-11-06.jsonl"
        assert path.exists()
        assert sink.excluded_paths() == (
            log_mod.ExcludedPath(log_dir.resolve(), "hook-2026-11-06.jsonl"),
        )
        assert "the decision log is busy" in capsys.readouterr().err
