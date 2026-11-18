"""Repository queries see every daily file the run wrote, and no others."""

from __future__ import annotations

import datetime as dt
import typing as typ

import pytest
from decision_log_support import _FixedClock, _resolver_failing_for, _RollingClock

from post_turn_quality_stop_hook import decision_log as log_mod

if typ.TYPE_CHECKING:
    from pathlib import Path


class TestExcludedPaths:
    """Repository queries ignore only files the sink created."""

    def test_nothing_is_excluded_before_a_write(self, tmp_path: Path) -> None:
        """A run that has not written yet hides nothing."""
        sink = log_mod.JsonlRecordSink(
            tmp_path / "log",
            clock=_FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0),
            is_tracked=lambda _candidate: False,
        )

        assert sink.excluded_paths() == ()

    def test_a_written_daily_file_is_excluded_exactly(self, tmp_path: Path) -> None:
        """The exclusion names the file, so a sibling daily name stays visible."""
        log_dir = tmp_path / "log"
        sink = log_mod.JsonlRecordSink(
            log_dir,
            clock=_FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0),
            is_tracked=lambda _candidate: False,
        )

        sink.write({"record_type": "start"})

        assert sink.excluded_paths() == (
            log_mod.ExcludedPath(log_dir.resolve(), "hook-2026-11-06.jsonl"),
        )

    def test_an_alias_log_directory_is_resolved_when_the_file_is_created(
        self, tmp_path: Path
    ) -> None:
        """The exclusion names the physical directory Git reports.

        Capturing the alias would leave the exclusion outside the worktree if
        the link could not be resolved at query time, and the run's own log
        file would then change the decision.
        """
        real = tmp_path / "real"
        real.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(real)
        log_dir = alias / "log"
        sink = log_mod.JsonlRecordSink(
            log_dir,
            clock=_FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0),
            is_tracked=lambda _candidate: False,
        )

        sink.write({"record_type": "start"})

        assert sink.excluded_paths() == (
            log_mod.ExcludedPath(real / "log", "hook-2026-11-06.jsonl"),
        )
        assert (real / "log" / "hook-2026-11-06.jsonl").is_file()

    def test_a_log_directory_that_cannot_be_resolved_refuses_the_record(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An exclusion Git cannot match means no record is written.

        The exclusion has to name the physical directory. If that cannot be
        resolved at creation, the run's own log could become visible to the
        gates, so the sink refuses before creating anything and the decision
        stays independent of logging.
        """
        log_dir = tmp_path / "log"
        sink = log_mod.JsonlRecordSink(
            log_dir,
            clock=_FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0),
            is_tracked=lambda _candidate: False,
        )
        monkeypatch.setattr(log_mod.Path, "resolve", _resolver_failing_for(log_dir))

        sink.write({"record_type": "start"})
        sink.write({"record_type": "progress"})

        assert sink.excluded_paths() == ()
        assert not (log_dir / "hook-2026-11-06.jsonl").exists()
        err = capsys.readouterr().err
        assert err.count("could not resolve the decision log directory") == 1

    def test_a_midnight_rollover_excludes_both_daily_files(
        self, tmp_path: Path
    ) -> None:
        """A run that crosses midnight wrote both files and may hide both."""
        log_dir = tmp_path / "log"
        rolling = _RollingClock(dt.datetime(2026, 11, 6, 23, 59, 50, tzinfo=dt.UTC))
        sink = log_mod.JsonlRecordSink(
            log_dir,
            clock=rolling,
            is_tracked=lambda _candidate: False,
        )

        sink.write({"record_type": "start"})
        rolling.advance(dt.timedelta(seconds=20))
        sink.write({"record_type": "progress"})

        assert sink.excluded_paths() == (
            log_mod.ExcludedPath(log_dir.resolve(), "hook-2026-11-06.jsonl"),
            log_mod.ExcludedPath(log_dir.resolve(), "hook-2026-11-07.jsonl"),
        )
