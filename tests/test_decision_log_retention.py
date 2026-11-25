"""Retention prunes expired daily files and keeps everything else."""

from __future__ import annotations

import datetime as dt
import os
import sys
import typing as typ

import pytest
from decision_log_support import (
    _hook_line_of_size,
    _log_line,
    _make_log,
    _read_records,
    _settings,
)
from git_support import _real_repository, _run_git

from post_turn_quality_stop_hook import decision_log as log_mod
from post_turn_quality_stop_hook import git_location as git_location_mod
from post_turn_quality_stop_hook.decision_log import prune_old_logs, prune_with_defaults
from post_turn_quality_stop_hook.decision_log import retention as retention_mod
from post_turn_quality_stop_hook.decision_log.constants import MAX_RECORD_BYTES

if typ.TYPE_CHECKING:
    from pathlib import Path

# The binding guarantee needs a directory-relative unlink, which Windows lacks.
POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="a directory-relative unlink is unavailable on Windows",
)


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

        removed = prune_with_defaults(log_dir, 7, today=today)

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

        removed = prune_with_defaults(log_dir, 0, today=today)

        assert removed == 1
        assert not yesterday.exists()
        assert current.exists()

    def test_negative_retention_is_ignored(self, tmp_path: Path) -> None:
        """A negative retention never deletes anything."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        old = log_dir / "hook-2020-01-01.jsonl"
        old.write_text(_log_line(), encoding="utf-8")

        assert prune_with_defaults(log_dir, -1, today=dt.date(2026, 11, 6)) == 0
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
        # Written with explicit LF endings: the default translation on Windows
        # would add a carriage return and push both fixtures over the limit, so
        # the exact-limit case would no longer be exactly at the limit.
        at_limit.write_text(
            _hook_line_of_size(MAX_RECORD_BYTES), encoding="utf-8", newline="\n"
        )
        over_limit.write_text(
            _hook_line_of_size(MAX_RECORD_BYTES + 1), encoding="utf-8", newline="\n"
        )

        removed = prune_with_defaults(log_dir, 7, today=dt.date(2026, 11, 6))

        assert removed == 1, f"expected one removal but {removed} happened"
        assert not at_limit.exists(), "a genuine record at the limit must be pruned"
        assert over_limit.exists(), (
            "a line over the limit is not provably the hook's and must be kept"
        )

    def test_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        """A missing log directory is reported as zero removals."""
        assert (
            prune_with_defaults(tmp_path / "absent", 14, today=dt.date(2026, 11, 6))
            == 0
        )

    def test_retention_beyond_the_date_range_keeps_files(self, tmp_path: Path) -> None:
        """A retention window past the calendar range prunes nothing."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        old = log_dir / "hook-2020-01-01.jsonl"
        old.write_text(_log_line(), encoding="utf-8")

        assert prune_with_defaults(log_dir, 10_000_000, today=dt.date(2026, 11, 6)) == 0
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

        log.retention.apply(10_000_000)
        log.finish()

        assert expired.exists()
        assert capsys.readouterr().err == ""
        assert _read_records(state_home)[0]["record_type"] == "start"


@POSIX_ONLY
@pytest.mark.parametrize(
    "entry_answer",
    [True, None],
    ids=["tracked", "unknown"],
)
def test_a_tracked_symlink_entry_is_preserved(
    tmp_path: Path, entry_answer: bool | None
) -> None:
    """Retention asks about the entry itself, not the link's target.

    The resolved-target checks see an untracked, ignored file and would permit
    the deletion. The unlink removes the symbolic link, though, and that entry
    is committed: deleting it would show as a tracked deletion in the worktree
    the uncommitted-changes gate reads. A tracked or unknown entry answer
    therefore preserves it.
    """
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    entry = log_dir / "hook-2020-01-01.jsonl"
    target = log_dir / "ignored-target.jsonl"
    target.write_text(_log_line(), encoding="utf-8")
    entry.symlink_to(target)

    removed = prune_old_logs(
        log_dir,
        7,
        today=dt.date(2026, 11, 6),
        is_tracked=lambda _candidate: False,
        is_tracked_entry=lambda _candidate: entry_answer,
        in_worktree=lambda _candidate: True,
        is_ignored=lambda _candidate: True,
    )

    assert removed == 0
    assert entry.is_symlink(), "a tracked entry must remain in the worktree"


@POSIX_ONLY
def test_an_untracked_symlink_entry_may_still_be_pruned(tmp_path: Path) -> None:
    """A confirmed untracked entry does not veto the resolved-target checks."""
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    entry = log_dir / "hook-2020-01-01.jsonl"
    target = log_dir / "ignored-target.jsonl"
    target.write_text(_log_line(), encoding="utf-8")
    entry.symlink_to(target)

    removed = prune_old_logs(
        log_dir,
        7,
        today=dt.date(2026, 11, 6),
        is_tracked=lambda _candidate: False,
        is_tracked_entry=lambda _candidate: False,
        in_worktree=lambda _candidate: True,
        is_ignored=lambda _candidate: True,
    )

    assert removed == 1
    assert not entry.exists()


@POSIX_ONLY
def test_a_tracked_symlink_at_a_daily_name_is_never_unlinked(tmp_path: Path) -> None:
    """A committed symlink survives a real retention sweep.

    This reproduces the defect with Git itself: the link's target is
    untracked and ignored, so the resolved-target checks approve the deletion,
    while ``git ls-files`` reports the link as a committed entry.
    """
    repo = _real_repository(tmp_path)
    logs = repo / "logs"
    logs.mkdir()
    (repo / ".gitignore").write_text("logs/\n", encoding="utf-8")
    target = logs / "expired.jsonl"
    target.write_text(_log_line(), encoding="utf-8")
    entry = repo / "hook-2020-01-01.jsonl"
    entry.symlink_to(target)
    _run_git(repo, "add", "hook-2020-01-01.jsonl")
    _run_git(repo, "commit", "-q", "-m", "track an expired daily log")

    assert git_location_mod.is_tracked(entry) is False, (
        "the resolved target is untracked; only the entry query can see the link"
    )
    assert git_location_mod.is_tracked_entry(entry) is True

    removed = prune_old_logs(
        repo,
        7,
        today=dt.date(2026, 11, 6),
        # The entry question is the rule under test, so it is the real Git
        # answer; the resolved-target questions are real too, because this
        # test reproduces the defect with Git itself.
        is_tracked=git_location_mod.is_tracked,
        is_tracked_entry=git_location_mod.is_tracked_entry,
        in_worktree=git_location_mod.is_in_worktree,
        is_ignored=git_location_mod.is_ignored,
    )

    assert removed == 0
    assert entry.is_symlink(), "the committed symlink must stay in the worktree"
    assert target.exists(), "the target is not retention's to remove either"


@POSIX_ONLY
def test_the_checked_entry_must_belong_to_the_swept_directory(
    tmp_path: Path,
) -> None:
    """A pathname that resolves elsewhere never authorises a deletion.

    Eligibility is decided from the pathname while the unlink runs through the
    directory descriptor. If a parent is renamed or replaced between those two
    operations, the file that is checked is not the file that is removed. A
    directory binding is required before either check can authorise the
    unlink, so the swept directory's entry always survives.
    """
    checked = tmp_path / "checked"
    swept = tmp_path / "swept"
    checked.mkdir()
    swept.mkdir()
    name = "hook-2020-01-01.jsonl"
    (checked / name).write_text(_log_line(), encoding="utf-8")
    (swept / name).write_text("not the hook's record\n", encoding="utf-8")

    descriptor = os.open(swept, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        removed = retention_mod._remove_verified(
            checked / name,
            None,
            can_delete=lambda _path: True,
            directory=descriptor,
        )
    finally:
        os.close(descriptor)

    assert removed is False
    assert (swept / name).read_text(encoding="utf-8") == "not the hook's record\n"
    assert (checked / name).exists()


@POSIX_ONLY
def test_eligibility_is_checked_through_the_opened_directory(
    tmp_path: Path,
) -> None:
    """A redirected alias cannot lend one directory's log to another's entry.

    The swept directory holds an unrelated file at the expired daily name,
    while another directory holds a valid hook log under that name. During the
    eligibility check the alias is moved to the valid log and then restored,
    so a check made through the pathname would approve the valid log while the
    unlink returned to the unrelated entry. Eligibility is decided through the
    descriptor, which still names the unrelated file, so that file survives.
    """
    swept = tmp_path / "swept"
    other = tmp_path / "other"
    swept.mkdir()
    other.mkdir()
    name = "hook-2020-01-01.jsonl"
    unrelated = swept / name
    unrelated.write_text("not the hook's record\n", encoding="utf-8")
    (other / name).write_text(_log_line(), encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(swept, target_is_directory=True)

    def can_delete(checked: Path) -> bool:
        """Decide eligibility with the alias pointed at the valid log.

        A pathname-based check would read ``other``'s file here and approve
        the deletion. The alias is restored before returning, so the later
        directory-binding check passes and only the anchored check can refuse.
        """
        alias.unlink()
        alias.symlink_to(other, target_is_directory=True)
        try:
            # All four answers permit, so only the anchored content check can
            # refuse; an omitted query no longer opts out of the check.
            return retention_mod._is_prunable(
                checked,
                is_tracked=lambda _candidate: False,
                is_tracked_entry=lambda _candidate: False,
                in_worktree=lambda _candidate: False,
                is_ignored=lambda _candidate: False,
            )
        finally:
            alias.unlink()
            alias.symlink_to(swept, target_is_directory=True)

    descriptor = os.open(swept, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        removed = retention_mod._remove_verified(
            alias / name,
            None,
            can_delete=can_delete,
            directory=descriptor,
        )
    finally:
        os.close(descriptor)

    assert removed is False
    assert unrelated.read_text(encoding="utf-8") == "not the hook's record\n", (
        "the entry the descriptor names must be the entry that is verified"
    )
    assert (other / name).exists()
