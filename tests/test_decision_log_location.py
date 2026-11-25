"""The sink writes only where Git cannot report the file.

Logging must never change the hook's decision. The uncommitted-changes gate
reports untracked entries, so the sink asks one question before it creates a
daily file: could a worktree report this path? Only a definite "no" permits a
write; a visible path, or an answer Git cannot give, is refused with one fixed
warning, and nothing is created or pruned.
"""

from __future__ import annotations

import datetime as dt
import sys
import typing as typ

import pytest
from decision_log_support import _FixedClock, _log_line, _RollingClock

from post_turn_quality_stop_hook import decision_log as log_mod
from post_turn_quality_stop_hook import git_location as git_location_mod

# The concrete sink and appender are adapters, exported separately from
# the decision log's domain-facing names.
from post_turn_quality_stop_hook.decision_log import adapters as adapters_mod
from post_turn_quality_stop_hook.decision_log.ownership import DescriptorOwnership

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.decision_log import append as append_mod

WALL = dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC)
LOG_NAME = "hook-2026-11-06.jsonl"
VISIBLE_WARNING = "could appear as a worktree change"
# The alias test needs symbolic links that can be replaced while the sink runs.
POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the alias test needs symbolic links",
)


def _sink(
    log_dir: Path,
    *,
    is_visible: typ.Callable[[Path], bool | None] | None = None,
    is_tracked: typ.Callable[[Path], bool | None] | None = None,
    clock: log_mod.Clock | None = None,
) -> adapters_mod.JsonlRecordSink:
    """Return a sink whose Git queries the caller controls."""
    tracking = is_tracked or (lambda _candidate: False)
    return adapters_mod.JsonlRecordSink(
        log_dir,
        clock=clock or _FixedClock(WALL, 0.0),
        is_tracked=tracking,
        is_visible=is_visible if is_visible is not None else (lambda _c: False),
        # These tests exercise the location question; the retention answers
        # describe an untracked file outside every worktree, so a sweep removes
        # the expired fixture the test provides rather than silently vetoing it.
        is_tracked_entry=tracking,
        in_worktree=lambda _candidate: False,
        is_ignored=lambda _candidate: False,
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
        """A caller that supplies no location answer still has Git decide.

        The sink no longer has an implicit location query, so the caller binds
        Git's own question explicitly. A location Git cannot answer for is
        refused exactly as an injected unknown answer is.
        """
        worktree = tmp_path / "repo"
        # An unresolvable marker makes the real query answer "unknown", which
        # is the only way to observe that it was asked at all.
        (worktree / ".git").mkdir(parents=True)
        log_dir = worktree / "log"
        sink = _sink(log_dir, is_visible=git_location_mod.would_appear_as_change)

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

    def test_visibility_is_settled_per_file_and_rechecked_before_creation(
        self, tmp_path: Path
    ) -> None:
        """Each daily file asks twice at creation, then nothing while it exists.

        The first question settles the pathname before anything is created.
        The second is asked immediately before the file is created, through the
        directory that will receive it, because the directory or the rule that
        ignored it may have changed since the first answer. Appending to a file
        that already exists asks nothing further, and a new day repeats the
        pair.
        """
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

        assert asked == [
            "hook-2026-11-06.jsonl",
            "hook-2026-11-06.jsonl",
            "hook-2026-11-07.jsonl",
            "hook-2026-11-07.jsonl",
        ], "creation re-asks; an existing daily file does not"

    def test_a_recreated_location_is_refused_after_the_ignore_rule_changes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A cached answer cannot re-create a log that stopped being ignored.

        A quality command can delete the state directory and remove the rule
        that ignored it during the same run. The earlier answer described the
        file that existed then; creation asks again, sees the directory is now
        visible, and refuses instead of re-creating the log where the gate
        would report it.
        """
        log_dir = tmp_path / "log"
        ignored = True

        def visible(_candidate: Path) -> bool | None:
            """Report the location as ignored until the test revokes that."""
            return not ignored

        sink = _sink(log_dir, is_visible=visible)
        sink.write({"record_type": "start"})
        assert (log_dir / LOG_NAME).exists()

        # The quality command removes the directory and its ignore rule.
        (log_dir / LOG_NAME).unlink()
        log_dir.rmdir()
        ignored = False

        sink.write({"record_type": "progress"})

        assert not (log_dir / LOG_NAME).exists(), (
            "a location that is no longer ignored must not be re-created"
        )
        assert VISIBLE_WARNING in capsys.readouterr().err

    def test_a_file_removed_after_the_existence_check_is_not_created(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Creation is decided at the creating open, not by an earlier answer.

        The sink skips ``may_create`` when the daily file already exists, and
        the appender then opens with ``O_CREAT``. A quality command that removes
        the file and clears the ignore rule between that existence check and the
        open would otherwise have the appender create a new daily file at a
        location the gates can now report, changing the uncommitted-changes
        gate. The created file is checked, and removed again, before any record
        is written to it.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / LOG_NAME
        path.write_text(_log_line(), encoding="utf-8")
        ignored = True

        def visible(_candidate: Path) -> bool | None:
            """Report the location as ignored until the test revokes that."""
            return not ignored

        class _VanishingAppender(adapters_mod.LockedJsonlAppender):
            """Remove the daily file after the sink has checked it exists."""

            def append(
                self,
                path: Path,
                line: str,
                policy: append_mod.AppendPolicy,
                *,
                location: Path | None = None,
            ) -> append_mod.AppendResult:
                """Drop the file and its ignore rule, then append as usual."""
                nonlocal ignored
                path.unlink()
                ignored = False
                return super().append(path, line, policy, location=location)

        clock = _FixedClock(WALL, 0.0)
        sink = adapters_mod.JsonlRecordSink(
            log_dir,
            clock=clock,
            is_tracked=lambda _candidate: False,
            is_visible=visible,
            is_tracked_entry=lambda _candidate: False,
            in_worktree=lambda _candidate: False,
            is_ignored=lambda _candidate: False,
            appender=_VanishingAppender(clock, ownership=DescriptorOwnership()),
        )

        sink.write({"record_type": "progress"})

        assert not path.exists(), (
            "the appender must not re-create a daily file after the location"
            " stopped being invisible"
        )
        assert VISIBLE_WARNING in capsys.readouterr().err

    @POSIX_ONLY
    def test_a_parent_alias_swapped_after_validation_does_not_receive_the_log(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Creation is anchored to the directory that passed the location check.

        The alias is moved to a worktree directory after the pathname was
        approved and before the file is created. The sink opens the directory
        and asks again through that handle, so the replacement directory is
        refused rather than receiving the new daily file.
        """
        external = tmp_path / "external"
        worktree = tmp_path / "worktree"
        (external / "log").mkdir(parents=True)
        (worktree / "log").mkdir(parents=True)
        alias = tmp_path / "state"
        alias.symlink_to(external, target_is_directory=True)

        def visible(candidate: Path) -> bool | None:
            """Report a path inside the worktree as visible to its gates."""
            return worktree in candidate.resolve().parents

        class _SwappingPolicy(log_mod.WritePolicy):
            """Move the alias once the pathname has been approved."""

            def settle_visibility(self, path: Path) -> log_mod.WriteVerdict:
                """Approve the original location, then redirect the alias.

                The predicate reports a verdict and does not itself refuse the
                run; the sink records the refusal through ``refuse_run``.
                """
                verdict = super().settle_visibility(path)
                alias.unlink()
                alias.symlink_to(worktree, target_is_directory=True)
                return verdict

        sink = adapters_mod.JsonlRecordSink(
            alias / "log",
            clock=_FixedClock(WALL, 0.0),
            is_tracked=lambda _candidate: False,
            is_visible=visible,
            is_tracked_entry=lambda _candidate: False,
            in_worktree=lambda _candidate: False,
            is_ignored=lambda _candidate: False,
            policy=_SwappingPolicy(is_tracked=lambda _c: False, is_visible=visible),
        )

        sink.write({"record_type": "start"})

        assert not (worktree / "log" / LOG_NAME).exists(), (
            "the redirected directory must not receive the daily file"
        )
        assert VISIBLE_WARNING in capsys.readouterr().err

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

    def test_a_refused_sink_never_prunes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A refused run leaves the directory exactly as it found it.

        The expired file carries a complete hook record, so a permitted sink
        would remove it. An empty file would prove nothing: retention refuses
        one whatever the sink asked for, and the test would then pass even with
        the refusal guard removed.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        expired = log_dir / "hook-2020-01-01.jsonl"
        expired.write_bytes(_log_line().encode("utf-8"))
        permitted = _sink(log_dir)
        permitted.prune(dt.date(2026, 11, 6), retention_days=1)
        assert not expired.exists(), (
            "the fixture must be one retention would remove, or the refusal"
            " below cannot be observed"
        )
        expired.write_bytes(_log_line().encode("utf-8"))
        refused = _sink(log_dir, is_visible=lambda _candidate: True)

        refused.write({"record_type": "start"})
        refused.prune(dt.date(2026, 11, 6), retention_days=1)

        assert expired.exists(), "a refused run must not delete anything"
        assert sorted(path.name for path in log_dir.iterdir()) == [expired.name]
        assert VISIBLE_WARNING in capsys.readouterr().err
