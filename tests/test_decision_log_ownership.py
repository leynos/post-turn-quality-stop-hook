"""Ownership is settled by record shape, never by the filename alone."""

from __future__ import annotations

import datetime as dt
import json
import typing as typ

import pytest
from decision_log_support import _log_line

from post_turn_quality_stop_hook.decision_log import prune_old_logs
from post_turn_quality_stop_hook.decision_log.constants import MAX_RECORD_BYTES

if typ.TYPE_CHECKING:
    from pathlib import Path


class TestRetentionOwnership:
    """Retention removes only files the hook itself created.

    A daily filename is not proof of ownership: an unrelated untracked file
    can carry the same name, and Git holds no copy of it. Retention therefore
    requires every line to carry the producer's own record shape, and leaves
    tracked files alone because deleting one would change the worktree. A file
    inside a worktree is preserved too, unless Git confirms it is ignored and
    so invisible to the gates.
    """

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "notes about this directory\n",
            "{}\n",
            '{"schema_version": "1", "record_type": "start"}\n',
            "[1, 2, 3]\n",
            "not json\n",
            # A schema-like envelope of another producer's own is not evidence
            # that this hook wrote the file.
            '{"schema_version": 1, "record_type": "customer_inventory"}\n',
            (
                '{"schema_version": 1, "producer": "other",'
                ' "record_type": "start", "run_id": "r", "pid": 1,'
                ' "timestamp": "t"}\n'
            ),
            (
                '{"schema_version": 1,'
                ' "producer": "post-turn-quality-stop-hook",'
                ' "record_type": "start"}\n'
            ),
            # A copied hook record does not make the rest of the file the
            # hook's own; deleting it would destroy data Git cannot restore.
            _log_line() + "unrelated data\n",
            # A torn final line cannot be verified as a complete record.
            _log_line()[:-1],
        ],
        ids=[
            "empty",
            "prose",
            "empty-object",
            "unversioned",
            "array",
            "invalid",
            "foreign-record-type",
            "foreign-producer",
            "incomplete-envelope",
            "hook-record-then-foreign-data",
            "torn-final-line",
        ],
    )
    def test_an_unrecognized_expired_file_is_preserved(
        self, tmp_path: Path, content: str
    ) -> None:
        """A file whose ownership cannot be established is never deleted."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        foreign = log_dir / "hook-2026-10-20.jsonl"
        foreign.write_text(content, encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            # Every Git answer permits, so only the record shape can preserve
            # the file.
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: False,
            is_ignored=lambda _path: False,
        )

        assert removed == 0
        assert foreign.exists()

    def test_a_large_genuine_log_is_recognised(self, tmp_path: Path) -> None:
        """Total size does not settle ownership; every line is verified.

        Verification bounds the memory it holds, not the size of the file. A
        long-running repository can legitimately produce a daily log of any
        size, and such a file must still expire on schedule.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        line = _log_line("progress")
        expired = log_dir / "hook-2026-10-20.jsonl"
        expired.write_text(line * (9 * 1024 * 1024 // len(line) + 1), encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: False,
            is_ignored=lambda _path: False,
        )

        assert removed == 1
        assert not expired.exists()

    def test_an_oversized_record_is_preserved(self, tmp_path: Path) -> None:
        """One unreasonably long line stays unverified rather than unbounded.

        The hook never writes a record approaching the per-line bound, so a
        line past it is foreign content. Rejecting it keeps peak memory
        independent of the file on disk.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        expired = log_dir / "hook-2026-10-20.jsonl"
        record = json.loads(_log_line())
        record["padding"] = "x" * (MAX_RECORD_BYTES + 1)
        expired.write_text(json.dumps(record) + "\n", encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: False,
            is_ignored=lambda _path: False,
        )

        assert removed == 0
        assert expired.exists()

    def test_a_deeply_nested_candidate_does_not_stop_the_sweep(
        self, tmp_path: Path
    ) -> None:
        """Recursion exhaustion rejects one candidate, not the whole pass.

        ``json.loads`` raises ``RecursionError`` on deeply nested input before
        any shape check can run. That file is foreign content and is preserved,
        and the genuine expired log beside it must still be removed: one
        unreadable candidate cannot make retention give up on the rest.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        nested = log_dir / "hook-2026-10-20.jsonl"
        nested.write_text("[" * 100_000 + "]" * 100_000 + "\n", encoding="utf-8")
        genuine = log_dir / "hook-2026-10-19.jsonl"
        genuine.write_text(_log_line(), encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: False,
            is_ignored=lambda _path: False,
        )

        assert removed == 1
        assert nested.exists(), "a candidate that exhausts the decoder is foreign"
        assert not genuine.exists(), "the sweep must continue past that candidate"

    def test_a_non_canonical_date_is_not_a_daily_file(self, tmp_path: Path) -> None:
        """A name the hook would never write is never treated as its own.

        ``fromisoformat`` also accepts the basic ISO form ``20261020``, which
        parses as a real date, but the hook only ever writes the extended form.
        Comparing the parsed date back against the encoded text is what rejects
        the basic form, so retention cannot delete a file whose name merely
        resembles a log file's.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        foreign = log_dir / "hook-20261020.jsonl"
        foreign.write_text(_log_line(), encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: False,
            is_ignored=lambda _path: False,
        )

        assert removed == 0
        assert foreign.exists()

    def test_a_non_string_record_type_is_preserved(self, tmp_path: Path) -> None:
        """An unhashable record type is foreign content, not a crash.

        Membership in the closed record-type set raises on a list or object,
        so the shape check must establish the type first. A file whose content
        cannot be read is preserved, because it is not the hook's to delete.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        foreign = log_dir / "hook-2026-10-20.jsonl"
        record = json.loads(_log_line())
        record["record_type"] = ["start"]
        foreign.write_text(json.dumps(record) + "\n", encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: False,
            is_ignored=lambda _path: False,
        )

        assert removed == 0
        assert foreign.exists()

    def test_a_directory_with_a_daily_name_is_preserved(self, tmp_path: Path) -> None:
        """A non-regular entry is never opened or deleted."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        directory = log_dir / "hook-2026-10-20.jsonl"
        directory.mkdir()

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            # Every Git answer permits, so only the entry type can preserve it.
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: False,
            is_ignored=lambda _path: False,
        )

        assert removed == 0
        assert directory.is_dir()

    def test_an_unknown_worktree_answer_preserves_the_record(
        self, tmp_path: Path
    ) -> None:
        """Only a confirmed position outside every worktree permits deletion."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        expired = log_dir / "hook-2026-10-20.jsonl"
        expired.write_text(_log_line(), encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: None,
            is_ignored=lambda _path: False,
        )

        assert removed == 0
        assert expired.exists()

    def test_a_file_of_copied_hook_records_inside_a_worktree_is_preserved(
        self, tmp_path: Path
    ) -> None:
        """Valid record contents alone do not prove ownership.

        An unrelated file can hold only well-formed hook records, so the
        producer marker cannot distinguish it. Its position inside a worktree
        can: retention never deletes repository content.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        expired = log_dir / "hook-2026-10-20.jsonl"
        expired.write_text(_log_line("start") + _log_line("finish"), encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: True,
            is_ignored=lambda _path: False,
        )

        assert removed == 0
        assert expired.exists()

    def test_an_ignored_log_inside_a_worktree_is_pruned(self, tmp_path: Path) -> None:
        """Ignored paths never reach either gate, so retention stays bounded.

        A state directory inside a repository would otherwise keep every daily
        file forever. Git confirms this one is ignored, so deleting it cannot
        change the changed-file list or the uncommitted-changes decision.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        expired = log_dir / "hook-2026-10-20.jsonl"
        expired.write_text(_log_line(), encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: True,
            is_ignored=lambda _path: True,
        )

        assert removed == 1
        assert not expired.exists()

    @pytest.mark.parametrize(
        ("tracking", "description"),
        [
            (True, "a committed file"),
            (None, "an unanswerable tracking query"),
        ],
        ids=["tracked", "unknown"],
    )
    def test_a_non_untracked_ignored_log_inside_a_worktree_is_preserved(
        self, tmp_path: Path, tracking: bool | None, description: str
    ) -> None:
        """Only a confirmed untracked answer outranks a confirmed ignore rule.

        An ignore rule and the index are independent: a file added with
        ``git add -f`` is tracked and matched by ``.gitignore`` at the same
        time. Deleting it would show as a deletion in the very worktree the
        gates read. When Git cannot answer the tracking question, the file is
        likewise preserved rather than deleted on the strength of the ignore
        answer alone.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        expired = log_dir / "hook-2026-10-20.jsonl"
        expired.write_text(_log_line(), encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: tracking,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: True,
            is_ignored=lambda _path: True,
        )

        assert removed == 0, f"{description} must not be deleted by retention"
        assert expired.exists()

    @pytest.mark.parametrize(
        (
            "is_tracked",
            "is_tracked_entry",
            "in_worktree",
            "is_ignored",
            "removed",
            "description",
        ),
        [
            (False, False, False, False, 1, "untracked outside every worktree"),
            (False, False, True, True, 1, "untracked and ignored inside a worktree"),
            (False, False, True, False, 0, "untracked but visible inside a worktree"),
            (True, False, False, False, 0, "tracked outside every worktree"),
            (None, False, False, False, 0, "unknown tracking outside a worktree"),
            (False, True, False, False, 0, "tracked entry outside every worktree"),
            (False, None, False, False, 0, "unknown entry tracking"),
            (False, False, None, False, 0, "unknown worktree position"),
            (False, False, True, None, 0, "unknown ignore answer"),
        ],
        ids=[
            "untracked-outside",
            "ignored-in-worktree",
            "visible-in-worktree",
            "tracked",
            "unknown-tracking",
            "tracked-entry",
            "unknown-entry",
            "unknown-worktree",
            "unknown-ignore",
        ],
    )
    def test_the_eligibility_matrix(  # ruff:ignore[too-many-arguments,too-many-positional-arguments]  # each query answer is an independent control.
        self,
        tmp_path: Path,
        is_tracked: bool | None,
        is_tracked_entry: bool | None,
        in_worktree: bool | None,
        is_ignored: bool | None,
        removed: int,
        description: str,
    ) -> None:
        """One matrix states every query combination retention may see.

        Each control starts from a genuine expired hook record and overrides
        one query, so the expected outcome isolates that rule. Only a confirmed
        untracked, unignored position may be deleted; every other answer,
        including an unknown one, preserves the file.
        """
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        expired = log_dir / "hook-2026-10-20.jsonl"
        expired.write_text(_log_line(), encoding="utf-8")

        actual = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: is_tracked,
            is_tracked_entry=lambda _path: is_tracked_entry,
            in_worktree=lambda _path: in_worktree,
            is_ignored=lambda _path: is_ignored,
        )

        assert actual == removed, f"{description} must remove {removed} files"
        assert expired.exists() is (removed == 0), (
            f"{description} must {'preserve' if removed == 0 else 'delete'} the file"
        )

    def test_a_non_ignored_log_inside_a_worktree_is_preserved(
        self, tmp_path: Path
    ) -> None:
        """Untracked repository content must survive to match a disabled run."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        expired = log_dir / "hook-2026-10-20.jsonl"
        expired.write_text(_log_line(), encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: True,
            is_ignored=lambda _path: False,
        )

        assert removed == 0
        assert expired.exists()

    def test_an_unknown_ignore_answer_preserves_the_record(
        self, tmp_path: Path
    ) -> None:
        """Only a confirmed ignored file may be deleted inside a worktree."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        expired = log_dir / "hook-2026-10-20.jsonl"
        expired.write_text(_log_line(), encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: True,
            is_ignored=lambda _path: None,
        )

        assert removed == 0
        assert expired.exists()

    def test_a_failing_ignore_query_preserves_the_record(self, tmp_path: Path) -> None:
        """An exception is not evidence that the file is ignored."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        expired = log_dir / "hook-2026-10-20.jsonl"
        expired.write_text(_log_line(), encoding="utf-8")

        def _fail(_path: Path) -> bool | None:
            """Simulate a Git query that cannot answer."""
            raise OSError

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: True,
            is_ignored=_fail,
        )

        assert removed == 0
        assert expired.exists()

    def test_an_expired_log_outside_every_worktree_is_pruned(
        self, tmp_path: Path
    ) -> None:
        """The normal XDG location still expires on schedule."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        expired = log_dir / "hook-2026-10-20.jsonl"
        expired.write_text(_log_line(), encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: False,
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: False,
            is_ignored=lambda _path: False,
        )

        assert removed == 1
        assert not expired.exists()

    def test_an_unknown_tracking_status_preserves_the_record(
        self, tmp_path: Path
    ) -> None:
        """Only a confirmed untracked answer permits deletion."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        expired = log_dir / "hook-2026-10-20.jsonl"
        expired.write_text(_log_line(), encoding="utf-8")

        removed = prune_old_logs(
            log_dir,
            7,
            today=dt.date(2026, 11, 6),
            is_tracked=lambda _path: None,
            # The remaining answers permit, so only the unknown tracking
            # answer can preserve the record.
            is_tracked_entry=lambda _path: False,
            in_worktree=lambda _path: False,
            is_ignored=lambda _path: False,
        )

        assert removed == 0
        assert expired.exists()
