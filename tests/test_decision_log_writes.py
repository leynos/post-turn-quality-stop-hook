"""The sink refuses files it does not own and reports retention failures."""

from __future__ import annotations

import datetime as dt
import typing as typ
from unittest import mock

import pytest
from decision_log_support import _FixedClock, _log_line

from post_turn_quality_stop_hook import decision_log as log_mod
from post_turn_quality_stop_hook.decision_log import prune_old_logs

if typ.TYPE_CHECKING:
    from pathlib import Path


class TestTrackedLogGuard:
    """A daily file Git tracks is never modified by the decision log.

    The start record is written before the uncommitted-changes gate runs.
    Appending to a tracked daily file would therefore turn a clean worktree
    dirty and change a stop that would pass with logging disabled.
    """

    def _tracked_sink(
        self,
        log_dir: Path,
        path: Path,
        *,
        on_error: typ.Callable[[str], None] | None = None,
        is_tracked: typ.Callable[[Path], bool | None] | None = None,
    ) -> log_mod.JsonlRecordSink:
        """Return a sink whose clock selects ``path``'s dated filename."""
        return log_mod.JsonlRecordSink(
            log_dir,
            clock=_FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0),
            on_error=on_error or log_mod._warn,
            is_tracked=is_tracked or (lambda candidate: candidate == path),
        )

    def test_a_tracked_daily_file_is_left_untouched(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The sink refuses to append and warns once, without touching stdout."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / "hook-2026-11-06.jsonl"
        path.write_text("committed content\n", encoding="utf-8")
        sink = self._tracked_sink(log_dir, path)

        sink.write({"record_type": "start"})
        sink.write({"record_type": "finish"})

        assert path.read_text(encoding="utf-8") == "committed content\n"
        captured = capsys.readouterr()
        assert captured.out == "", "stdout stays reserved for the hook JSON"
        assert captured.err.count("tracked by Git") == 1, (
            "the refusal is reported once per run"
        )

    def test_a_refused_sink_does_not_prune(self, tmp_path: Path) -> None:
        """Retention stays inert once the sink has refused to write."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / "hook-2026-11-06.jsonl"
        path.write_text("committed content\n", encoding="utf-8")
        expired = log_dir / "hook-2026-10-01.jsonl"
        expired.write_text(_log_line(), encoding="utf-8")
        messages: list[str] = []
        sink = self._tracked_sink(
            log_dir,
            path,
            on_error=messages.append,
            # Only the daily file is tracked; the expired record is not.
            is_tracked=lambda candidate: candidate == path,
        )

        sink.write({"record_type": "start"})
        sink.prune(dt.date(2026, 11, 6), 7)

        assert expired.exists()
        assert messages == [
            "the decision log file is tracked by Git; not writing to it"
        ]

    def test_a_failing_tracking_query_refuses_to_write(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unanswerable ownership question protects the decision."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / "hook-2026-11-06.jsonl"

        def _raise(_candidate: Path) -> bool | None:
            """Simulate a tracking query that cannot answer."""
            message = "git unavailable"
            raise RuntimeError(message)

        sink = self._tracked_sink(log_dir, path, is_tracked=_raise)

        sink.write({"record_type": "start"})

        assert not path.exists(), "an unresolved tracking query must not risk a write"
        assert "could not determine whether the decision log file is tracked" in (
            capsys.readouterr().err
        )

    def test_an_unknown_tracking_answer_refuses_to_write(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ``None`` answer is not permission: the sink stays inert."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / "hook-2026-11-06.jsonl"

        sink = self._tracked_sink(log_dir, path, is_tracked=lambda _candidate: None)

        sink.write({"record_type": "start"})

        assert not path.exists()
        assert "could not determine whether the decision log file is tracked" in (
            capsys.readouterr().err
        )

    def test_an_untracked_foreign_file_is_not_appended_to(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A daily name does not make a pre-existing file the hook's own.

        Appending would modify data the hook does not own, and excluding the
        file would hide it from the uncommitted-changes gate.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / "hook-2026-11-06.jsonl"
        path.write_text('{"schema_version": 1, "record_type": "inventory"}\n')
        sink = self._tracked_sink(log_dir, path, is_tracked=lambda _candidate: False)

        sink.write({"record_type": "start"})

        assert path.read_text(encoding="utf-8") == (
            '{"schema_version": 1, "record_type": "inventory"}\n'
        )
        assert sink.excluded_paths() == ()
        assert "was not written by this hook" in capsys.readouterr().err

    def test_a_file_with_foreign_data_after_a_hook_record_is_not_appended_to(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every line must be a hook record, not only the first.

        A copied or stray hook record followed by unrelated data is not the
        hook's file. Appending would mix two writers into one line stream, so
        the file is preserved untouched.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / "hook-2026-11-06.jsonl"
        content = _log_line() + "unrelated data\n"
        path.write_text(content, encoding="utf-8")
        sink = self._tracked_sink(log_dir, path, is_tracked=lambda _candidate: False)

        sink.write({"record_type": "start"})

        assert path.read_text(encoding="utf-8") == content
        assert sink.excluded_paths() == ()
        assert "was not written by this hook" in capsys.readouterr().err

    def test_an_empty_daily_file_is_claimed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty file holds no content, so a crashed run's file is reused.

        A run killed between creating the file and writing its first record
        leaves it empty. Refusing that file would disable logging for the rest
        of the day. An empty daily file is also excluded: only a
        logging-enabled run creates one, so a logging-disabled run would never
        have reported it to the gates either.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / "hook-2026-11-06.jsonl"
        path.touch()
        sink = self._tracked_sink(log_dir, path, is_tracked=lambda _candidate: False)

        sink.write({"record_type": "start"})

        assert path.read_text(encoding="utf-8") == '{"record_type":"start"}\n'
        assert sink.excluded_paths() == (
            log_mod.ExcludedPath(log_dir.resolve(), path.name),
        )
        assert capsys.readouterr().err == ""

    def test_a_pre_existing_hook_file_is_appended_and_excluded(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An earlier run's daily file is reused and hidden from the gates.

        A logging-disabled run never creates a daily file, so the hook treats
        a file it can verify as its own as a logging artefact whether this run
        or an earlier one wrote it. Excluding it is what keeps a
        logging-enabled run reporting the same worktree as one with logging
        off.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / "hook-2026-11-06.jsonl"
        first = (
            '{"schema_version":1,"producer":"post-turn-quality-stop-hook",'
            '"record_type":"start","run_id":"a","pid":1,'
            '"timestamp":"2026-11-06T00:00:00+00:00"}\n'
        )
        path.write_text(first, encoding="utf-8")
        sink = self._tracked_sink(log_dir, path, is_tracked=lambda _candidate: False)

        sink.write({"record_type": "progress"})

        assert (
            path.read_text(encoding="utf-8") == first + '{"record_type":"progress"}\n'
        )
        assert sink.excluded_paths() == (
            log_mod.ExcludedPath(log_dir.resolve(), path.name),
        )
        assert capsys.readouterr().err == ""


class TestRetentionDiagnostics:
    """Retention failures are reported with fixed, path-free messages."""

    def test_scan_failure_is_reported(self, tmp_path: Path) -> None:
        """A directory that cannot be scanned reports one fixed message."""
        messages: list[str] = []
        with mock.patch.object(log_mod.Path, "iterdir", side_effect=OSError("boom")):
            removed = prune_old_logs(
                tmp_path, 14, today=dt.date(2026, 11, 6), on_error=messages.append
            )

        assert removed == 0
        assert messages == ["could not scan the decision-log directory"]

    def test_delete_failure_is_reported(self, tmp_path: Path) -> None:
        """A file that cannot be removed reports one fixed message."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        (log_dir / "hook-2020-01-01.jsonl").write_text(_log_line(), encoding="utf-8")
        messages: list[str] = []

        with mock.patch.object(log_mod.Path, "unlink", side_effect=OSError("boom")):
            removed = prune_old_logs(
                log_dir, 1, today=dt.date(2026, 11, 6), on_error=messages.append
            )

        assert removed == 0
        assert messages == ["could not remove an expired decision-log file"]

    def test_jsonl_sink_reports_retention_failures_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The default JSONL sink routes retention failures to stderr."""
        sink = log_mod.JsonlRecordSink(tmp_path / "log")
        with mock.patch.object(log_mod.Path, "iterdir", side_effect=OSError("boom")):
            sink.prune(dt.date(2026, 11, 6), 14)

        captured = capsys.readouterr()
        assert "could not scan the decision-log directory" in captured.err
        assert captured.out == "", "stdout stays reserved for the hook JSON"
