"""The sink writes only where Git cannot report the file.

Logging must never change the hook's decision. The uncommitted-changes gate
reports untracked entries, so the sink asks one question before it creates a
daily file: could a worktree report this path? Only a definite "no" permits a
write; a visible path, or an answer Git cannot give, is refused with one fixed
warning, and nothing is created or pruned.
"""

from __future__ import annotations

import datetime as dt
import typing as typ

import pytest
from decision_log_support import _FixedClock, _RollingClock

from post_turn_quality_stop_hook import decision_log as log_mod

if typ.TYPE_CHECKING:
    from pathlib import Path

WALL = dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC)
LOG_NAME = "hook-2026-11-06.jsonl"
VISIBLE_WARNING = "could appear as a worktree change"


def _sink(
    log_dir: Path,
    *,
    is_visible: typ.Callable[[Path], bool | None] | None = None,
    is_tracked: typ.Callable[[Path], bool | None] | None = None,
    clock: log_mod.Clock | None = None,
) -> log_mod.JsonlRecordSink:
    """Return a sink whose Git queries the caller controls."""
    return log_mod.JsonlRecordSink(
        log_dir,
        clock=clock or _FixedClock(WALL, 0.0),
        is_tracked=is_tracked or (lambda _candidate: False),
        is_visible=is_visible if is_visible is not None else (lambda _c: False),
    )


class TestLogLocation:
    """A daily file is created only where the gates cannot see it."""

    def test_a_visible_location_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A path a worktree could report is never written."""
        log_dir = tmp_path / "log"
        sink = _sink(log_dir, is_visible=lambda _candidate: True)

        sink.write({"record_type": "start"})
        sink.write({"record_type": "finish"})

        assert not (log_dir / LOG_NAME).exists()
        captured = capsys.readouterr()
        assert captured.out == "", "stdout stays reserved for the hook JSON"
        assert captured.err.count(VISIBLE_WARNING) == 1, (
            "the refusal is reported once per run, not once per record"
        )

    def test_an_ignored_or_outside_worktree_location_is_written(
        self, tmp_path: Path
    ) -> None:
        """A location no worktree can report receives the record."""
        log_dir = tmp_path / "log"
        sink = _sink(log_dir)

        sink.write({"record_type": "start"})

        assert (log_dir / LOG_NAME).read_text(encoding="utf-8").strip() == (
            '{"record_type":"start"}'
        )

    def test_an_unknown_location_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An answer Git cannot give refuses the write rather than risking it."""
        log_dir = tmp_path / "log"
        sink = _sink(log_dir, is_visible=lambda _candidate: None)

        sink.write({"record_type": "start"})

        assert not (log_dir / LOG_NAME).exists()
        assert "could not determine whether" in capsys.readouterr().err

    def test_a_sink_built_without_a_location_query_asks_git(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Omitting the query does not mean "permit": Git still decides.

        The default is the real Git question, so a location Git cannot answer
        for is refused exactly as an injected unknown answer is.
        """
        worktree = tmp_path / "repo"
        # An unresolvable marker makes the default query answer "unknown",
        # which is the only way to observe that it was asked at all.
        (worktree / ".git").mkdir(parents=True)
        log_dir = worktree / "log"
        sink = log_mod.JsonlRecordSink(
            log_dir,
            clock=_FixedClock(WALL, 0.0),
            is_tracked=lambda _candidate: False,
        )

        sink.write({"record_type": "start"})

        assert not (log_dir / LOG_NAME).exists()
        assert "could not determine whether the decision log location" in (
            capsys.readouterr().err
        )

    def test_a_raising_query_is_treated_as_unknown(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A query that raises refuses the write instead of escaping the hook."""

        def broken(_candidate: Path) -> bool | None:
            """Fail the way a substituted query might."""
            raise RuntimeError

        log_dir = tmp_path / "log"
        sink = _sink(log_dir, is_visible=broken)

        sink.write({"record_type": "start"})

        assert not (log_dir / LOG_NAME).exists()
        assert "could not determine whether" in capsys.readouterr().err

    def test_visibility_is_asked_once_per_daily_file(self, tmp_path: Path) -> None:
        """A run asks once per file, and asks again after midnight."""
        asked: list[str] = []
        log_dir = tmp_path / "log"
        rolling = _RollingClock(dt.datetime(2026, 11, 6, 23, 59, 50, tzinfo=dt.UTC))
        sink = _sink(
            log_dir,
            clock=rolling,
            is_visible=lambda path: asked.append(path.name) or False,
        )

        sink.write({"record_type": "start"})
        sink.write({"record_type": "progress"})
        rolling.advance(dt.timedelta(seconds=20))
        sink.write({"record_type": "finish"})

        assert asked == ["hook-2026-11-06.jsonl", "hook-2026-11-07.jsonl"], (
            "the location of each daily file is settled once"
        )

    def test_a_tracked_file_is_refused_before_the_location_question(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Writing to a tracked file is refused without asking about location."""
        asked: list[Path] = []

        def visible(path: Path) -> bool | None:
            """Record that the location question was reached."""
            asked.append(path)
            return False

        log_dir = tmp_path / "log"
        sink = _sink(
            log_dir,
            is_tracked=lambda _candidate: True,
            is_visible=visible,
        )

        sink.write({"record_type": "start"})

        assert asked == [], "the tracked file is refused first"
        assert not (log_dir / LOG_NAME).exists()
        assert "tracked by Git" in capsys.readouterr().err

    def test_a_refused_sink_never_prunes(self, tmp_path: Path) -> None:
        """A refused run leaves the directory exactly as it found it."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        expired = log_dir / "hook-2020-01-01.jsonl"
        expired.write_text("", encoding="utf-8")
        sink = _sink(log_dir, is_visible=lambda _candidate: True)

        sink.write({"record_type": "start"})
        sink.prune(dt.date(2026, 11, 6), retention_days=1)

        assert expired.exists(), "a refused run must not delete anything"
        assert sorted(path.name for path in log_dir.iterdir()) == [expired.name]
