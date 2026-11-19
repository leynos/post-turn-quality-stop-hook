"""The record schema and the lifecycle of a single run."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import typing as typ
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest
from decision_log_support import (
    LOG_FILE_MODE,
    _finish_record,
    _FixedClock,
    _hook_line_of_size,
    _log_files,
    _log_line,
    _make_log,
    _read_records,
    _RecordingSink,
    _settings,
)

from post_turn_quality_stop_hook import decision_log as log_mod
from post_turn_quality_stop_hook.decision_log import (
    MAX_REASON_CHARS,
    DecisionLog,
    DecisionLogSettings,
)
from post_turn_quality_stop_hook.decision_log import assembly as assembly_mod
from post_turn_quality_stop_hook.decision_log.assembly import RunRecord
from post_turn_quality_stop_hook.decision_log.constants import (
    MAX_CORRELATION_CHARS,
    MAX_RECORD_BYTES,
    RECORD_HEADROOM_BYTES,
)

# The constructor writes the start record and ``finish`` writes one more, so a
# run that cannot write either attempts exactly two writes.
_RECORD_ATTEMPTS_PER_RUN = 2
# One run is truncated, the other completes: two start records share the file.
_RUNS_IN_FILE = 2
# Larger than one whole record may be, so a bound that did not exist would let
# the value through and make every record carrying it unverifiable.
_OVERSIZED_CORRELATION_CHARS = 2 * MAX_RECORD_BYTES
# A blocked hook exits non-zero, and the finish record must preserve that code
# even after detail is dropped to fit the reader's limit.
_BLOCK_EXIT_CODE = 2
# The writer's margin below the reader's per-line limit, shared by the record
# assembly and the sink.
_WRITE_LIMIT = MAX_RECORD_BYTES - RECORD_HEADROOM_BYTES


def _finish_body_length(pad: int) -> int:
    """Return the encoded length of a bare finish record carrying ``pad`` bytes.

    JSON encodes a run of ``x`` without escaping, so the length grows by
    exactly ``pad``. Measuring the empty case therefore sizes a test record
    without writing anything.
    """
    run = RunRecord(log_mod.SystemClock(), started=0.0)
    run.set_quality({"pad": "x" * pad})
    record = run.finish(decision="allow")
    assert record is not None
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return len(encoded.encode("utf-8"))


class _InterruptingSink(_RecordingSink):
    """Sink that runs a callback after each record is delivered.

    The callback stands in for a signal handler, which cannot otherwise be
    made to land reliably in the narrow window between building a record and
    writing it.
    """

    def __init__(
        self, on_written: typ.Callable[[typ.Mapping[str, object]], None]
    ) -> None:
        """Start empty and keep the delivery callback."""
        super().__init__()
        self._on_written = on_written

    def write(self, record: typ.Mapping[str, object]) -> None:
        """Keep the record, then run the callback."""
        super().write(record)
        self._on_written(record)


class TestRecords:
    """Records carry the documented fields and no command output."""

    def test_short_write_rolls_back_and_the_next_run_recovers(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A partial record is removed, so a later invocation can still append.

        A short write would leave a torn line that no reader can verify, and
        every later append would then have to refuse the file. The adapter
        writes each record once, warns, and restores the verified length, so a
        fresh sink must still find a valid stream and append to it.
        """
        settings = _settings(tmp_path)
        log = _make_log(settings)
        real_write = os.write

        def write_prefix(descriptor: int, data: bytes) -> int:
            """Write the first byte of ``data``, as a short write does."""
            return real_write(descriptor, data[:1])

        with mock.patch.object(log_mod.os, "write", side_effect=write_prefix) as write:
            log.finish()

        assert write.call_count == _RECORD_ATTEMPTS_PER_RUN, (
            "each record is attempted once, never retried"
        )
        assert "could not write the complete decision log record" in (
            capsys.readouterr().err
        )
        path = _log_files(tmp_path / "state")[0]
        assert path.read_text(encoding="utf-8").endswith("\n")
        assert len(_read_records(tmp_path / "state")) == 1, "only the start survives"

        survivor = _make_log(settings)
        survivor.finish()

        records = _read_records(tmp_path / "state")
        starts = sum(record["record_type"] == "start" for record in records)
        assert starts == _RUNS_IN_FILE, (
            "the truncated run's start record and the survivor's must both"
            f" survive, but {starts} start records were readable"
        )
        assert _finish_record(records)["run_id"] == survivor.run_id

    def test_undecodable_command_is_escaped_instead_of_lost(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A command holding undecodable bytes is escaped, not dropped.

        A value decoded with ``surrogateescape`` cannot be written as strict
        UTF-8 with ``ensure_ascii=False``. Dropping the record would lose the
        command, so serialisation falls back to ASCII escaping: the line stays
        strict UTF-8, the value round-trips, and the size bound measures the
        same escaped bytes the writer persists.
        """
        log = _make_log(_settings(tmp_path))

        log.command_start("make", "tool-\udcff", ["all"])

        assert capsys.readouterr().err == "", "an escapable value is not a failure"
        commands = [
            record
            for record in _read_records(tmp_path / "state")
            if record.get("event") == "command_start"
        ]
        assert commands, "the undecodable command record must survive"
        command = typ.cast("dict[str, object]", commands[-1]["command"])
        assert command["cmd"] == "tool-\udcff"

    def test_start_record_carries_correlation(self, tmp_path: Path) -> None:
        """The start record joins the run to its Claude Code transcript."""
        log = _make_log(_settings(tmp_path))

        records = _read_records(tmp_path / "state")

        start = records[0]
        assert start["record_type"] == "start"
        assert start["run_id"] == log.run_id
        assert start["session_id"] == "session-1"
        assert start["transcript_path"] == "/logs/transcript.jsonl"
        assert start["hook_event_name"] == "Stop"
        assert start["stop_hook_active"] is False
        assert start["hook_version"]
        assert start["start_dir"] == str(Path("/repo"))
        assert start["base_ref"] == "origin/main"
        assert start["pid"] == os.getpid()

    def test_correlation_known_after_construction_is_recorded(
        self, tmp_path: Path
    ) -> None:
        """The payload's correlation is durable as soon as it is known."""
        log = DecisionLog(
            _settings(tmp_path),
            log_mod.InvocationContext(
                correlation={},
                start_dir=Path("/repo"),
                base_ref="origin/main",
            ),
        )

        log.set_correlation({"session_id": "session-1"})
        log.finish()

        records = _read_records(tmp_path / "state")
        assert "session_id" not in records[0], (
            "the start record predates the payload read"
        )
        correlation = next(
            record for record in records if record.get("event") == "correlation"
        )
        assert correlation["session_id"] == "session-1"
        finish = _finish_record(records)
        assert finish["session_id"] == "session-1"

    def test_an_outsized_correlation_value_is_bounded(self, tmp_path: Path) -> None:
        """A huge payload identifier cannot make the daily file unverifiable.

        Correlation values are copied into every later record, so an identifier
        larger than the reader's whole per-line limit would make a genuine file
        foreign: later runs would refuse to append and retention would never
        remove it. Ordinary identifiers stay intact; only an outsized value is
        truncated.
        """
        oversized = "s" * _OVERSIZED_CORRELATION_CHARS
        log = DecisionLog(
            _settings(tmp_path),
            log_mod.InvocationContext(
                correlation={"session_id": oversized},
                start_dir=Path("/repo"),
                base_ref="origin/main",
            ),
        )
        log.set_correlation({"transcript_path": oversized})
        log.finish()

        records = _read_records(tmp_path / "state")
        start_id = typ.cast("str", records[0]["session_id"])
        assert len(start_id) == MAX_CORRELATION_CHARS, (
            f"the payload identifier was not bounded: {len(start_id)} characters"
        )
        correlation = next(
            record for record in records if record.get("event") == "correlation"
        )
        late_path = typ.cast("str", correlation["transcript_path"])
        assert len(late_path) == MAX_CORRELATION_CHARS, (
            f"the later identifier was not bounded: {len(late_path)} characters"
        )

        # A fresh run is the real proof: it can only append if the file still
        # verifies as the hook's own.
        survivor = _make_log(_settings(tmp_path))
        survivor.finish()
        starts = sum(
            record["record_type"] == "start"
            for record in _read_records(tmp_path / "state")
        )
        assert starts == _RUNS_IN_FILE, (
            f"the bounded file was not reopened: {starts} start records"
        )

    def test_a_daily_file_at_the_reader_limit_is_still_appended_to(
        self, tmp_path: Path
    ) -> None:
        """The reader's per-line limit is inclusive, so a boundary file is ours.

        Verification rejects a line only when it is longer than the limit. A
        file whose longest line is exactly the limit must therefore still be
        recognised, or a writer at its documented bound would lock every later
        run out of the day's file.
        """
        settings = _settings(tmp_path)
        log_dir = log_mod.log_directory(settings.state_home)
        log_dir.mkdir(parents=True)
        today = dt.datetime.now(dt.UTC).date().isoformat()
        (log_dir / f"hook-{today}.jsonl").write_text(
            _hook_line_of_size(MAX_RECORD_BYTES), encoding="utf-8", newline=""
        )

        log = _make_log(settings)
        log.finish()

        records = _read_records(tmp_path / "state")
        assert any("pad" in record for record in records), (
            "the boundary record disappeared from the daily file"
        )
        assert _finish_record(records)["run_id"] == log.run_id

    def test_an_oversized_record_is_dropped_and_the_file_stays_verifiable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The writer enforces the reader's limit before appending anything.

        A record over the limit would not verify as a hook record, so the next
        run would refuse the whole file. Dropping that one record leaves the
        decision untouched and the stream readable.
        """
        settings = _settings(tmp_path)
        log = _make_log(settings)
        sink = log_mod.JsonlRecordSink(log_mod.log_directory(settings.state_home))

        sink.write({
            "record_type": "progress",
            "event": "correlation",
            "pad": "x" * MAX_RECORD_BYTES,
        })
        log.finish()

        err = capsys.readouterr().err
        assert "could not write the decision log (RecordTooLarge)" in err, (
            f"an oversized record must be reported, but stderr was {err!r}"
        )
        write = sink.storage_stats()["write"]
        assert write["error_categories"] == ["RecordTooLarge"], (
            f"the bound failure was not counted: {write!r}"
        )
        records = _read_records(tmp_path / "state")
        assert not any("pad" in record for record in records), (
            "the oversized record reached the daily file"
        )
        assert _finish_record(records)["run_id"] == log.run_id

    def test_stage_progress_is_written_on_start_and_finish(
        self, tmp_path: Path
    ) -> None:
        """A stage records progress before its work begins."""
        log = _make_log(_settings(tmp_path))

        with log.stage("prepare"):
            progress = _read_records(tmp_path / "state")

        assert progress[-1]["event"] == "stage_start"
        assert progress[-1]["stage"] == "prepare"
        log.finish()

        finish = _finish_record(_read_records(tmp_path / "state"))
        stages = typ.cast("dict[str, object]", finish["stages"])
        assert isinstance(stages["prepare"], int)
        events = [
            record.get("event")
            for record in _read_records(tmp_path / "state")
            if record.get("event") is not None
        ]
        # Storage telemetry is its own progress record, written by ``finish``
        # so the terminal decision stays the last line of the file.
        assert events == ["stage_start", "stage_finish", "storage"]

    def test_command_records_exclude_output(self, tmp_path: Path) -> None:
        """Commands record exit status and timing, never captured output."""
        log = _make_log(_settings(tmp_path))
        progress = log.command_start("code", "make test", ["test"])

        in_flight = _read_records(tmp_path / "state")
        assert in_flight[-1]["event"] == "command_start"
        log.command_finish(progress, 2)
        log.finish()

        finish = _finish_record(_read_records(tmp_path / "state"))
        commands = typ.cast("list[dict[str, object]]", finish["commands"])
        command = commands[0]
        assert command == {
            "kind": "code",
            "cmd": "make test",
            "targets": ["test"],
            "exit_code": 2,
            "duration_ms": command["duration_ms"],
        }
        serialized = json.dumps(finish)
        assert "stdout" not in serialized
        assert "stderr" not in serialized

    def test_finish_lists_every_gate_in_order(self, tmp_path: Path) -> None:
        """Unreached gates are recorded explicitly, not omitted."""
        log = _make_log(_settings(tmp_path))
        log.record_gate("quality_checks", "pass")
        log.record_gate("uncommitted_changes", "skip", "gate disabled")
        log.finish()

        finish = _finish_record(_read_records(tmp_path / "state"))

        gates = typ.cast("list[dict[str, object]]", finish["gates"])
        assert [gate["gate"] for gate in gates] == list(log_mod.GATE_ORDER)
        assert gates[0]["outcome"] == "pass"
        assert gates[1] == {
            "gate": "uncommitted_changes",
            "outcome": "skip",
            "reason": "gate disabled",
        }
        assert gates[2]["outcome"] == "not_reached"
        assert gates[3]["outcome"] == "not_reached"

    def test_mark_block_sets_decision_and_bounded_reason(self, tmp_path: Path) -> None:
        """A block decision records a single-line, bounded reason."""
        log = _make_log(_settings(tmp_path))
        reason = "line one\nline two " + "x" * MAX_REASON_CHARS

        log.mark_block(reason)
        log.finish()

        finish = _finish_record(_read_records(tmp_path / "state"))
        assert finish["decision"] == "block"
        bounded = typ.cast("str", finish["block_reason"])
        assert "\n" not in bounded
        assert len(bounded) == MAX_REASON_CHARS

    def test_finish_records_error_and_exit_code(self, tmp_path: Path) -> None:
        """Unexpected errors are summarized without a traceback."""
        log = _make_log(_settings(tmp_path))

        log.finish(decision="error", exit_code=1, error="ValueError: boom")

        finish = _finish_record(_read_records(tmp_path / "state"))
        assert finish["decision"] == "error"
        assert finish["exit_code"] == 1
        assert finish["error"] == "ValueError: boom"

    def test_finish_is_idempotent(self, tmp_path: Path) -> None:
        """A second finish call does not append another record."""
        log = _make_log(_settings(tmp_path))

        log.finish()
        log.finish()

        records = _read_records(tmp_path / "state")
        assert sum(record["record_type"] == "finish" for record in records) == 1

    def test_a_signal_after_assembly_still_delivers_an_interrupted_finish(
        self, tmp_path: Path
    ) -> None:
        """A signal between building and writing the finish is still recorded.

        Assembly marks that the terminal record was built, not that it reached
        the sink. A signal handler that runs while the record is being
        delivered must be able to write the interrupted record instead of
        finding the run already finished and writing nothing.
        """
        stats = log_mod.StorageStats()
        signal_delivered = False

        def _signal_on_storage(record: typ.Mapping[str, object]) -> None:
            """Deliver SIGTERM once, as the storage telemetry reaches the sink."""
            nonlocal signal_delivered
            if signal_delivered or record.get("event") != "storage":
                return
            signal_delivered = True
            log.record_signal(signal.SIGTERM)

        sink = _InterruptingSink(_signal_on_storage)
        log = DecisionLog(
            _settings(tmp_path),
            log_mod.InvocationContext(
                correlation={}, start_dir=Path("/repo"), base_ref="origin/main"
            ),
            deps=log_mod.DecisionLogDeps(
                sink=sink,
                storage=stats,
                clock=_FixedClock(dt.datetime(2026, 11, 6, tzinfo=dt.UTC), 0.0),
                is_tracked=lambda _candidate: False,
                is_visible=lambda _candidate: False,
            ),
        )
        # The storage record is written before the finish record, so a signal
        # raised while handling it lands inside the delivery window.
        stats.start_write()
        log.finish()

        finishes = [
            record for record in sink.records if record["record_type"] == "finish"
        ]
        assert [record["decision"] for record in finishes] == ["interrupted"], (
            "the signal handler must deliver the terminal record it observes"
        )
        assert finishes[0]["signal"] == "SIGTERM"

    def test_each_record_is_a_single_json_line(self, tmp_path: Path) -> None:
        """Records are appended one line at a time for concurrent readers."""
        log = _make_log(_settings(tmp_path))
        log.set_repository(branch="main")
        log.set_quality(build_driver="make")
        log.finish()

        path = _log_files(tmp_path / "state")[0]
        text = path.read_text(encoding="utf-8")

        # Every record is terminated by its own newline, so a reader never has
        # to guess whether the last line is complete.
        assert text.endswith("\n")
        lines = text.split("\n")[:-1]
        records = _read_records(tmp_path / "state")
        assert len(lines) == len(records)
        assert records[0]["record_type"] == "start"
        assert records[-1]["record_type"] == "finish"
        for line in lines:
            assert isinstance(json.loads(line), dict)

    def test_a_record_with_an_embedded_line_separator_stays_one_line(
        self, tmp_path: Path
    ) -> None:
        """A U+2028 inside a value must not tear the record for a reader.

        JSON permits U+2028 inside a string and the writer does not escape it.
        A reader that split on ``str.splitlines`` would divide the record there
        and fail to parse either half, so record boundaries are newlines only.
        """
        log = _make_log(_settings(tmp_path))
        log.set_correlation({"note": "before\u2028after"})
        log.finish()

        records = _read_records(tmp_path / "state")
        correlation = next(
            record for record in records if record.get("event") == "correlation"
        )
        assert correlation["note"] == "before\u2028after"

    def test_write_failure_warns_without_raising(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An unwritable location never changes the hook decision."""
        blocker = tmp_path / "state"
        blocker.write_text("not a directory", encoding="utf-8")
        settings = DecisionLogSettings(state_home=blocker)

        log = _make_log(settings)
        log.finish()

        err = capsys.readouterr().err
        assert "could not write the decision log" in err
        assert str(blocker) not in err, "the warning must not expose the state path"

    @pytest.mark.skipif(
        os.name != "posix", reason="file permission bits are POSIX-specific"
    )
    def test_an_existing_world_readable_log_is_tightened(self, tmp_path: Path) -> None:
        """An existing file is chmodded so records are never group-readable."""
        log = _make_log(_settings(tmp_path))
        log.set_repository(branch="main")
        path = _log_files(tmp_path / "state")[0]
        path.chmod(0o644)

        log.finish()

        assert path.stat().st_mode & 0o777 == LOG_FILE_MODE

    def test_concurrent_threads_do_not_interleave(self, tmp_path: Path) -> None:
        """Records from threads sharing one process remain parseable.

        The threads contend for the sink's in-process lock. Cross-process
        appends are defended by ``O_APPEND`` and the file lock, but this test
        exercises threads only: it does not establish cross-process behaviour.
        """
        settings = _settings(tmp_path)

        def run_one(_index: int) -> str:
            """Write one run's records through its own log instance."""
            log = _make_log(settings)
            log.record_gate("quality_checks", "pass")
            log.finish()
            return log.run_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            run_ids = list(pool.map(run_one, range(24)))

        records = _read_records(tmp_path / "state")
        # Each run writes a start record, a storage record, and a finish
        # record. A storage record is present because every run appended.
        assert len(records) == len(run_ids) * 3
        assert len(set(run_ids)) == len(run_ids)
        starts = {
            record["run_id"] for record in records if record["record_type"] == "start"
        }
        finishes = {
            record["run_id"] for record in records if record["record_type"] == "finish"
        }
        assert starts == finishes == set(run_ids)

    def test_retention_runs_once_the_window_is_resolved(self, tmp_path: Path) -> None:
        """Applying retention prunes files older than the retention window."""
        state_home = tmp_path / "state"
        log_dir = log_mod.log_directory(state_home)
        log_dir.mkdir(parents=True)
        today = dt.datetime.now(dt.UTC).date()
        stale = log_dir / f"hook-{today - dt.timedelta(days=30)}.jsonl"
        stale.write_text(_log_line(), encoding="utf-8")
        recent = log_dir / f"hook-{today - dt.timedelta(days=1)}.jsonl"
        recent.write_text(_log_line(), encoding="utf-8")

        log = _make_log(_settings(tmp_path))

        assert stale.exists(), (
            "construction must not prune before the configured window is known"
        )
        log.apply_retention(14)
        assert not stale.exists(), "a fourteen-day window removes the stale file"
        assert recent.exists(), "a fourteen-day window keeps yesterday's file"

        log.apply_retention(0)
        assert not recent.exists(), "a zero-day window keeps only today's file"


class TestFinishBound:
    """A finish record is bounded as it is written."""

    def test_a_finish_record_over_the_limit_drops_optional_detail(self) -> None:
        """A bulky finish record loses detail rather than the decision.

        The reader treats an over-limit line as foreign, so the finish record
        must fit the same bound the writer enforces. The command log is the
        first detail dropped, and the record names what went so a reader can
        tell a truncated record from a small invocation. The decision, exit
        code and gate outcomes always survive.
        """
        run = RunRecord(log_mod.SystemClock(), started=0.0)
        run.add_command({"command": "x" * MAX_RECORD_BYTES})
        run.gate("quality_checks", "pass")

        record = run.finish(decision="block", exit_code=_BLOCK_EXIT_CODE)

        assert record is not None
        assert record["decision"] == "block"
        assert record["exit_code"] == _BLOCK_EXIT_CODE
        omitted = record["details_omitted"]
        assert omitted == ["commands"], (
            f"expected only the command log to go, but got {omitted!r}"
        )
        gates = typ.cast("list[dict[str, object]]", record["gates"])
        assert gates[0]["outcome"] == "pass", "gate outcomes must survive the bound"
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        assert len(encoded.encode("utf-8")) <= MAX_RECORD_BYTES, (
            "the bounded finish record must stay inside the reader's limit"
        )

    def test_the_finish_bound_covers_the_written_record(self, tmp_path: Path) -> None:
        """The bound applies to the record as written, not only its body.

        ``RunRecord`` bounds the record it assembles, but ``DecisionLog`` adds
        the common fields afterwards. A body that fits on its own can exceed
        the writer's limit once those fields are merged, and the sink would
        then drop the one record that carries the decision. The quality
        section is what gives way, and the decision still reaches the file.
        """
        # Leave the body a few bytes inside its own bound, so only the merged
        # common fields can push the complete record past the writer's limit.
        body_room = _WRITE_LIMIT - _finish_body_length(0)
        pad = body_room - 10
        log = _make_log(_settings(tmp_path))
        log.set_quality(pad="x" * pad)

        log.finish(decision="allow")

        finish = _finish_record(_read_records(tmp_path / "state"))
        assert finish["decision"] == "allow", "the decision must survive the bound"
        assert finish["details_omitted"] == ["commands", "quality"], (
            "the empty command list goes first, then the oversized quality"
            f" section: {finish['details_omitted']!r}"
        )
        assert "quality" not in finish, "the dropped section must not be written"
        path = _log_files(tmp_path / "state")[0]
        longest = max(
            len(line.encode("utf-8"))
            for line in path.read_text(encoding="utf-8").splitlines()
        )
        assert longest <= _WRITE_LIMIT, (
            f"a written record is {longest} bytes, over the writer's {_WRITE_LIMIT}"
        )

    def test_both_bounding_passes_keep_their_omissions(self, tmp_path: Path) -> None:
        """A second pass names every section that was dropped.

        The body bound and the written bound each drop sections. The later
        pass must carry the earlier list forward instead of replacing it, or a
        reader cannot tell that the command log was truncated before the
        quality section.
        """
        base = _finish_body_length(0)
        # Overshoot the body bound by five bytes: the empty command list goes
        # first, then the quality section that carries the padding, and the
        # written pass must not forget the first omission.
        pad = (_WRITE_LIMIT - base) + 5
        log = _make_log(_settings(tmp_path))
        log.set_quality(pad="x" * pad)

        log.finish(decision="allow")

        finish = _finish_record(_read_records(tmp_path / "state"))
        assert finish["decision"] == "allow"
        assert finish["details_omitted"] == ["commands", "quality"], (
            f"the later pass lost an omission: {finish['details_omitted']!r}"
        )

    def test_an_omission_label_that_cannot_fit_is_left_out(self) -> None:
        """The label never pushes a record past the limit it exists to meet.

        Once the only droppable section is gone, naming it can still cost more
        bytes than the record has left. Writing the record without the label
        keeps the decision; writing the label would make the sink drop the
        record entirely.
        """
        record: dict[str, object] = {"commands": [], "pad": ""}
        overhead = len(json.dumps(record, separators=(",", ":")).encode())
        record["pad"] = "x" * (_WRITE_LIMIT - overhead + 5)

        bounded = assembly_mod._bounded(record)

        assert "commands" not in bounded, "the oversized section must still go"
        assert "details_omitted" not in bounded, (
            "a label that cannot fit must not be written"
        )
        encoded = json.dumps(bounded, separators=(",", ":")).encode()
        assert len(encoded) <= _WRITE_LIMIT, (
            f"the bare record is {len(encoded)} bytes, over the writer's limit"
        )

    def test_the_real_sink_accepts_a_record_whose_label_cannot_fit(
        self, tmp_path: Path
    ) -> None:
        """The bare record reaches the real writer rather than being dropped.

        The unit case above shows the label is left out; this one shows the
        record is still within the writer's own limit, so the sink writes it
        instead of rejecting it as oversized and losing the decision.
        """
        record: dict[str, object] = {
            "record_type": "finish",
            "decision": "allow",
            "commands": [],
            "pad": "",
        }
        overhead = len(json.dumps(record, separators=(",", ":")).encode())
        record["pad"] = "x" * (_WRITE_LIMIT - overhead)
        labelled = json.dumps(
            {**record, "details_omitted": ["commands"]}, separators=(",", ":")
        )
        assert len(labelled.encode("utf-8")) > _WRITE_LIMIT, (
            "the case only means anything if the label cannot fit"
        )
        bounded = assembly_mod._bounded(record)
        assert "details_omitted" not in bounded, (
            "a record with nothing omitted must not grow the label"
        )

        sink = log_mod.JsonlRecordSink(
            log_mod.log_directory(tmp_path / "state"),
            clock=_FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0),
            is_tracked=lambda _candidate: False,
            is_visible=lambda _candidate: False,
        )
        sink.write(bounded)

        (path,) = _log_files(tmp_path / "state")
        (line,) = path.read_text(encoding="utf-8").splitlines()
        written = typ.cast("dict[str, object]", json.loads(line))
        assert written["decision"] == "allow", "the decision must reach the file"
        assert "details_omitted" not in written
        assert len(line.encode("utf-8")) <= _WRITE_LIMIT
