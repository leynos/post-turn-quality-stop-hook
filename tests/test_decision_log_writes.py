"""The sink refuses files it does not own and reports retention failures."""

from __future__ import annotations

import datetime as dt
import typing as typ
from pathlib import Path
from unittest import mock

import pytest
from decision_log_support import _FixedClock, _log_line

from post_turn_quality_stop_hook import decision_log as log_mod

# The default sink and its Git-bound pruning factory are adapters; the
# decision log package exports the domain-facing names only.
from post_turn_quality_stop_hook.decision_log import adapters as adapters_mod
from post_turn_quality_stop_hook.decision_log import retention as retention_mod
from post_turn_quality_stop_hook.diagnostics import warn

# One existing fixture record plus the single record this append writes.
_EXPECTED_FIXTURE_AND_RECORD_LINES = 2


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
    ) -> adapters_mod.JsonlRecordSink:
        """Return a sink whose clock selects ``path``'s dated filename."""
        tracking = is_tracked or (lambda candidate: candidate == path)
        return adapters_mod.JsonlRecordSink(
            log_dir,
            clock=_FixedClock(dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC), 0.0),
            on_error=on_error or warn,
            is_tracked=tracking,
            # These tests are about ownership, so they place the log where no
            # worktree can report it and the location check cannot interfere.
            is_visible=lambda _candidate: False,
            # Answer the retention questions for an untracked file outside every
            # worktree, so an expired fixture stays prunable and only the
            # refusal guard under test can preserve it.
            is_tracked_entry=tracking,
            in_worktree=lambda _candidate: False,
            is_ignored=lambda _candidate: False,
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
            raise OSError(message)

        sink = self._tracked_sink(log_dir, path, is_tracked=_raise)

        sink.write({"record_type": "start"})

        assert not path.exists(), "an unresolved tracking query must not risk a write"
        assert "could not determine whether the decision log file is tracked" in (
            capsys.readouterr().err
        )

    def test_a_tracked_hook_owned_file_refuses_the_write(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ownership does not outrank Git: a tracked file stays untouched.

        A daily file that already holds valid hook records passes the record
        shape check. If Git tracks it, appending would modify committed content
        and make a clean worktree dirty before the gate runs, so the tracking
        answer must refuse it on its own.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / "hook-2026-11-06.jsonl"
        path.write_text(_log_line(), encoding="utf-8")
        original = path.read_text(encoding="utf-8")
        sink = self._tracked_sink(log_dir, path)

        sink.write({"record_type": "start"})

        assert path.read_text(encoding="utf-8") == original, (
            "a tracked file the hook wrote earlier still must not be appended to"
        )
        assert "tracked by Git" in capsys.readouterr().err

    def test_the_tracking_question_reaches_the_owned_file(self, tmp_path: Path) -> None:
        """The tracking question is asked about the daily entry being appended.

        A parent pathname can be replaced between an earlier check and the
        open, so the answer must describe the entry reached through the opened
        directory. Every candidate names today's daily file, and the appended
        record lands in it.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / "hook-2026-11-06.jsonl"
        path.write_text(_log_line(), encoding="utf-8")
        asked: list[Path] = []

        def is_tracked(candidate: Path) -> bool | None:
            """Record each candidate the sink asks about, reporting untracked."""
            asked.append(candidate)
            return False

        sink = self._tracked_sink(log_dir, path, is_tracked=is_tracked)

        sink.write({"record_type": "start"})

        assert asked, "the append must ask the tracking question"
        assert {candidate.name for candidate in asked} == {path.name}, (
            f"every question must name today's daily entry, saw {asked!r}"
        )
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == _EXPECTED_FIXTURE_AND_RECORD_LINES, (
            "the owned, untracked file receives the record"
        )
        assert '"record_type":"start"' in lines[-1], (
            "the appended record landed in the file the question described"
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

        Appending would modify data the hook does not own, so the sink refuses
        the file and leaves it untouched.
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
        assert "was not written by this hook" in capsys.readouterr().err

    def test_an_empty_daily_file_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A pre-existing empty file is not proven to be the hook's, so it stays.

        An empty file carries no record, so it proves nothing about who made
        it. Claiming it would let this run treat a file it cannot account for
        as its own, so the sink refuses it and leaves it untouched.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        path = log_dir / "hook-2026-11-06.jsonl"
        path.touch()
        sink = self._tracked_sink(log_dir, path, is_tracked=lambda _candidate: False)

        sink.write({"record_type": "start"})

        assert path.read_text(encoding="utf-8") == ""
        assert "was not written by this hook" in capsys.readouterr().err

    def test_a_pre_existing_hook_file_is_appended(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An earlier run's daily file is reused.

        A file made entirely of hook records is the hook's own whether this
        run or an earlier one wrote it, so the sink appends to it. Reuse keeps
        one daily file per day.
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
        assert capsys.readouterr().err == ""


class TestPolicyPredicates:
    """Predicates answer questions; only the refusal command changes state.

    A predicate must be safe to ask speculatively: it cannot write a warning or
    refuse the run, because the caller has not yet acted on the answer. The
    sink records a refusal explicitly through ``refuse_run``.
    """

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            (True, log_mod.RefusalReason.FILE_TRACKED),
            (None, log_mod.RefusalReason.TRACKING_UNKNOWN),
        ],
        ids=["tracked", "unknown"],
    )
    def test_permits_write_does_not_refuse_or_warn(
        self,
        capsys: pytest.CaptureFixture[str],
        answer: bool | None,
        expected: log_mod.RefusalReason,
    ) -> None:
        """Answering the write question leaves the warning and state untouched."""
        messages: list[str] = []
        policy = log_mod.WritePolicy(
            on_error=messages.append,
            is_tracked=lambda _candidate: answer,
            # Both tracking answers refuse before the location is asked.
            is_visible=lambda _candidate: False,
        )

        verdict = policy.permits_write(Path("/log/hook-2026-11-06.jsonl"))

        assert verdict.permitted is False
        assert verdict.refusal is expected
        assert policy.refused is False, "a predicate must not refuse the run"
        assert messages == [], "a predicate must not write to standard error"
        assert capsys.readouterr().err == ""

        policy.refuse_run(verdict.refusal)
        assert policy.refused is True
        assert len(messages) == 1
        assert messages[0].startswith(expected.value)
        assert messages[0].endswith("; not writing to it")

    def test_permits_write_writes_no_instance_state(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The write predicate is read-only, including its visibility cache.

        ``permits_write`` answers a question about one path. It must not fill
        the policy's visibility cache, refuse the run, or warn: caching is a
        separate, explicitly named command
        (:meth:`WritePolicy.settle_visibility`), so a caller that only asks a
        question cannot change a later answer.
        """
        asked: list[str] = []
        messages: list[str] = []
        policy = log_mod.WritePolicy(
            on_error=messages.append,
            is_tracked=lambda _candidate: False,
            is_visible=lambda candidate: asked.append(candidate.name) or False,
        )
        path = Path("/log/hook-2026-11-06.jsonl")

        first = policy.permits_write(path)
        second = policy.permits_write(path)

        assert first.permitted is True
        assert second.permitted is True
        assert asked == [path.name, path.name], (
            "a read-only query must ask Git again rather than reuse a cache"
        )
        assert policy.refused is False
        assert messages == []
        assert capsys.readouterr().err == ""

    def test_settle_visibility_caches_only_when_asked_to(self) -> None:
        """Only the explicitly named command makes a later settle reuse one."""
        asked: list[str] = []
        policy = log_mod.WritePolicy(
            on_error=lambda _message: None,
            is_tracked=lambda _candidate: False,
            is_visible=lambda candidate: asked.append(candidate.name) or False,
        )
        path = Path("/log/hook-2026-11-06.jsonl")

        policy.settle_visibility(path)
        policy.settle_visibility(path)

        assert asked == [path.name], (
            "settling caches the answer, so a second settle reuses it"
        )

        policy.permits_write(path)

        assert asked == [path.name, path.name], (
            "the read-only query asks Git afresh; it never consumes the cache"
        )

    def test_may_create_does_not_refuse_or_warn(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Answering the creation question leaves the warning and state alone."""
        messages: list[str] = []
        policy = log_mod.WritePolicy(
            on_error=messages.append,
            # ``may_create`` asks only the location question.
            is_tracked=lambda _candidate: False,
            is_visible=lambda _candidate: True,
        )

        verdict = policy.may_create(Path("/log/hook-2026-11-06.jsonl"))

        assert verdict.permitted is False
        assert verdict.refusal is log_mod.RefusalReason.LOCATION_VISIBLE
        assert policy.refused is False
        assert messages == []
        assert capsys.readouterr().err == ""

    def test_refuse_run_emits_one_fixed_message_per_run(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The refusal command reports once, whatever its reason or count."""
        messages: list[str] = []
        policy = log_mod.WritePolicy(
            on_error=messages.append,
            # The refusal command asks neither Git question.
            is_tracked=lambda _candidate: False,
            is_visible=lambda _candidate: False,
        )

        policy.refuse_run(log_mod.RefusalReason.TRACKING_UNKNOWN)
        policy.refuse_run(log_mod.RefusalReason.LOCATION_VISIBLE)

        assert messages == [
            (
                "could not determine whether the decision log file is tracked by"
                " Git; not writing to it"
            )
        ], "later refusals must not add a second warning"
        assert capsys.readouterr().err == ""

    def test_predicates_after_a_refusal_report_no_new_reason(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A refused run answers every predicate without a second refusal."""
        messages: list[str] = []
        policy = log_mod.WritePolicy(
            on_error=messages.append,
            is_tracked=lambda _candidate: False,
            is_visible=lambda _candidate: False,
        )
        policy.refuse_run(log_mod.RefusalReason.FILE_TRACKED)

        verdicts = (
            policy.permits_write(Path("/log/hook-2026-11-06.jsonl")),
            policy.may_create(Path("/log/hook-2026-11-06.jsonl")),
        )

        assert all(not verdict.permitted for verdict in verdicts)
        assert all(verdict.refusal is None for verdict in verdicts), (
            "an already-refused run has no new reason to report"
        )
        assert len(messages) == 1
        assert capsys.readouterr().err == ""

    def test_a_query_failure_refuses_the_write_through_its_reason(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A Git query that raises ``OSError`` withholds the answer, not the reason.

        The failure becomes the same unknown answer the verdict already
        describes, so acting on the verdict reports the fixed refusal reason
        and no write can be attempted on a repository Git could not answer for.
        """
        messages: list[str] = []
        policy = log_mod.WritePolicy(
            on_error=messages.append,
            is_tracked=_raise_os_error,
            is_visible=lambda _candidate: False,
        )
        path = Path("/log/hook-2026-11-06.jsonl")

        verdict = policy.permits_write(path)

        assert verdict.permitted is False
        assert verdict.refusal is log_mod.RefusalReason.TRACKING_UNKNOWN, (
            f"expected an unknown tracking answer but got {verdict.refusal!r}"
        )
        policy.refuse_run(verdict.refusal)
        assert messages == [
            (
                "could not determine whether the decision log file is tracked by"
                " Git; not writing to it"
            )
        ], f"the failure must be reported through the refusal reason, got {messages!r}"
        assert capsys.readouterr().err == ""

    def test_an_unexpected_query_defect_propagates(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An error the query is not documented to raise is never swallowed.

        Mapping every exception to ``None`` would hide a defect behind an
        ordinary refusal, so only the query's documented ``OSError`` becomes an
        unknown answer.
        """
        policy = log_mod.WritePolicy(
            on_error=lambda _message: None,
            is_tracked=_raise_value_error,
            is_visible=lambda _candidate: False,
        )

        with pytest.raises(ValueError, match="a programming defect"):
            policy.permits_write(Path("/log/hook-2026-11-06.jsonl"))

        assert capsys.readouterr().err == ""


def _raise_os_error(_candidate: Path) -> bool | None:
    """Fail the query the way a Git command that cannot run does."""
    message = "git could not run"
    raise OSError(message)


def _raise_value_error(_candidate: Path) -> bool | None:
    """Fail the query the way a programming defect does."""
    message = "a programming defect"
    raise ValueError(message)


class TestRetentionDiagnostics:
    """Retention failures are reported with fixed, path-free messages."""

    def test_scan_failure_is_reported(self, tmp_path: Path) -> None:
        """A directory that cannot be scanned reports one fixed message."""
        messages: list[str] = []
        with mock.patch.object(Path, "iterdir", side_effect=OSError("boom")):
            removed = adapters_mod.prune_with_defaults(
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

        # Patch the deletion helper itself: on POSIX the file is removed by a
        # directory-relative ``os.unlink``, so patching ``Path.unlink`` would
        # not exercise the failure branch at all.
        with mock.patch.object(retention_mod, "_unlink", side_effect=OSError("boom")):
            removed = adapters_mod.prune_with_defaults(
                log_dir, 1, today=dt.date(2026, 11, 6), on_error=messages.append
            )

        assert removed == 0
        assert messages == ["could not remove an expired decision-log file"]

    def test_jsonl_sink_reports_retention_failures_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The default JSONL sink routes retention failures to stderr."""
        sink = adapters_mod.default_jsonl_sink(tmp_path / "log")
        with mock.patch.object(Path, "iterdir", side_effect=OSError("boom")):
            sink.prune(dt.date(2026, 11, 6), 14)

        captured = capsys.readouterr()
        assert "could not scan the decision-log directory" in captured.err
        assert captured.out == "", "stdout stays reserved for the hook JSON"
