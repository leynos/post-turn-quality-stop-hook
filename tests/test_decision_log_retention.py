"""Retention prunes expired daily files and keeps everything else."""

from __future__ import annotations

import datetime as dt
import typing as typ

import pytest
from decision_log_support import (
    _hook_line_of_size,
    _log_line,
    _make_log,
    _read_records,
    _settings,
)

from post_turn_quality_stop_hook import decision_log as log_mod
from post_turn_quality_stop_hook.decision_log import prune_old_logs
from post_turn_quality_stop_hook.decision_log.constants import MAX_RECORD_BYTES

if typ.TYPE_CHECKING:
    from pathlib import Path


class TestRetention:
    """Prune expired daily files."""

    def test_prunes_only_expired_log_files(self, tmp_path: Path) -> None:
        """Old dated files are removed; the cutoff and every later file stay."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        today = dt.date(2026, 11, 6)
        # Seven days before 2026-11-06 is 2026-10-30, which is still inside the
        # window: only dates strictly before the cutoff expire.
        cutoff = log_dir / "hook-2026-10-30.jsonl"
        just_expired = log_dir / "hook-2026-10-29.jsonl"
        today_file = log_dir / f"hook-{today.isoformat()}.jsonl"
        recent = log_dir / "hook-2026-11-01.jsonl"
        unrelated = log_dir / "notes.txt"
        malformed = log_dir / "hook-not-a-date.jsonl"
        for path in (cutoff, just_expired, today_file, recent):
            path.write_text(_log_line(), encoding="utf-8")
        for path in (unrelated, malformed):
            path.write_text("", encoding="utf-8")

        removed = prune_old_logs(log_dir, 7, today=today)

        assert removed == 1
        assert not just_expired.exists()
        assert cutoff.exists(), "a file exactly on the cutoff is still retained"
        assert today_file.exists()
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

    def test_an_expired_file_at_the_reader_limit_is_pruned(
        self, tmp_path: Path
    ) -> None:
        """A line exactly at the reader's limit still verifies and is removed.

        Retention and append share the record validator, whose per-line limit
        is inclusive. Placing a genuine record on that boundary proves a file
        the writer bounds correctly is never stranded past its expiry, while a
        line one byte longer remains foreign and is preserved.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        at_limit = log_dir / "hook-2026-10-01.jsonl"
        over_limit = log_dir / "hook-2026-10-02.jsonl"
        at_limit.write_text(_hook_line_of_size(MAX_RECORD_BYTES), encoding="utf-8")
        over_limit.write_text(
            _hook_line_of_size(MAX_RECORD_BYTES + 1), encoding="utf-8"
        )

        removed = prune_old_logs(log_dir, 7, today=dt.date(2026, 11, 6))

        assert removed == 1, f"expected one removal but {removed} happened"
        assert not at_limit.exists(), "a genuine record at the limit must be pruned"
        assert over_limit.exists(), (
            "a line over the limit is not provably the hook's and must be kept"
        )

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

    def test_a_huge_retention_window_leaves_an_expired_log_alone(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An extreme retention value prunes nothing and reports nothing.

        ``today - 10_000_000 days`` lies outside the representable date range,
        so no cutoff can be computed. The retention pass must keep the expired
        log and stay quiet rather than aborting the run.
        """
        state_home = tmp_path / "state"
        log = _make_log(_settings(tmp_path, retention_days=10_000_000))
        expired = log_mod.log_directory(state_home) / "hook-2020-01-01.jsonl"
        expired.write_text(_log_line(), encoding="utf-8")

        log.apply_retention(10_000_000)
        log.finish()

        assert expired.exists()
        assert capsys.readouterr().err == ""
        assert _read_records(state_home)[0]["record_type"] == "start"
