"""The record schema and the lifecycle of a single run."""

from __future__ import annotations

import datetime as dt
import json
import os
import typing as typ
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest
from decision_log_support import (
    LOG_FILE_MODE,
    _finish_record,
    _log_files,
    _log_line,
    _make_log,
    _read_records,
    _settings,
)

from post_turn_quality_stop_hook import decision_log as log_mod
from post_turn_quality_stop_hook.decision_log import (
    MAX_REASON_CHARS,
    DecisionLog,
    DecisionLogSettings,
)


class TestRecords:
    """Records carry the documented fields and no command output."""

    def test_short_write_warns_without_retrying(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A partial append warns instead of silently truncating a record."""
        log = _make_log(_settings(tmp_path))
        with mock.patch.object(log_mod.os, "write", return_value=1) as write:
            log.finish()

        assert write.call_count == 1, "a short write must not be retried"
        assert "could not write the complete decision log record" in (
            capsys.readouterr().err
        )

    def test_undecodable_command_warns_instead_of_raising(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A command holding undecodable bytes cannot change the decision."""
        log = _make_log(_settings(tmp_path))

        log.command_start("make", "tool-\udcff", ["all"])

        assert "could not write the decision log" in capsys.readouterr().err

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
        assert start["start_dir"] == "/repo"
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
        assert events == ["stage_start", "stage_finish"]

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

    def test_each_record_is_a_single_json_line(self, tmp_path: Path) -> None:
        """Records are appended one line at a time for concurrent readers."""
        log = _make_log(_settings(tmp_path))
        log.set_repository(branch="main")
        log.set_quality(build_driver="make")
        log.finish()

        path = _log_files(tmp_path / "state")[0]
        lines = path.read_text(encoding="utf-8").splitlines()

        assert len(lines) == len(_read_records(tmp_path / "state"))
        for line in lines:
            assert isinstance(json.loads(line), dict)

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
        ``O_APPEND`` atomicity is covered by the end-to-end hook tests, which
        run real subprocesses.
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
        assert len(records) == len(run_ids) * 2
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
