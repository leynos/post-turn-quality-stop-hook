"""Retention prunes expired daily files and keeps everything else."""

from __future__ import annotations

import datetime as dt
import typing as typ

from decision_log_support import (
    _log_line,
    _make_log,
    _read_records,
    _settings,
)

from post_turn_quality_stop_hook.decision_log import prune_old_logs

if typ.TYPE_CHECKING:
    from pathlib import Path


class TestRetention:
    """Prune expired daily files."""

    def test_prunes_only_expired_log_files(self, tmp_path: Path) -> None:
        """Old dated files are removed; recent and unrelated files stay."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        today = dt.date(2026, 11, 6)
        expired = log_dir / "hook-2026-10-20.jsonl"
        boundary = log_dir / f"hook-{today.isoformat()}.jsonl"
        recent = log_dir / "hook-2026-11-01.jsonl"
        unrelated = log_dir / "notes.txt"
        malformed = log_dir / "hook-not-a-date.jsonl"
        for path in (expired, boundary, recent):
            path.write_text(_log_line(), encoding="utf-8")
        for path in (unrelated, malformed):
            path.write_text("", encoding="utf-8")

        removed = prune_old_logs(log_dir, 7, today=today)

        assert removed == 1
        assert not expired.exists()
        assert boundary.exists()
        assert recent.exists()
        assert unrelated.exists()
        assert malformed.exists()

    def test_zero_retention_keeps_only_today(self, tmp_path: Path) -> None:
        """A zero-day retention removes every earlier file."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        today = dt.date(2026, 11, 6)
        yesterday = log_dir / "hook-2026-11-05.jsonl"
        current = log_dir / "hook-2026-11-06.jsonl"
        yesterday.write_text(_log_line(), encoding="utf-8")
        current.write_text(_log_line(), encoding="utf-8")

        removed = prune_old_logs(log_dir, 0, today=today)

        assert removed == 1
        assert not yesterday.exists()
        assert current.exists()

    def test_negative_retention_is_ignored(self, tmp_path: Path) -> None:
        """A negative retention never deletes anything."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        old = log_dir / "hook-2020-01-01.jsonl"
        old.write_text(_log_line(), encoding="utf-8")

        assert prune_old_logs(log_dir, -1, today=dt.date(2026, 11, 6)) == 0
        assert old.exists()

    def test_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        """A missing log directory is reported as zero removals."""
        assert prune_old_logs(tmp_path / "absent", 14, today=dt.date(2026, 11, 6)) == 0

    def test_retention_beyond_the_date_range_keeps_files(self, tmp_path: Path) -> None:
        """A retention window past the calendar range prunes nothing."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        old = log_dir / "hook-2020-01-01.jsonl"
        old.write_text(_log_line(), encoding="utf-8")

        assert prune_old_logs(log_dir, 10_000_000, today=dt.date(2026, 11, 6)) == 0
        assert old.exists()

    def test_construction_with_huge_retention_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        """An extreme retention value never aborts log construction."""
        log = _make_log(_settings(tmp_path, retention_days=10_000_000))

        log.finish()

        assert _read_records(tmp_path / "state")[0]["record_type"] == "start"
