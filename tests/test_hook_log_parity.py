"""End-to-end parity tests for where the decision log may be written.

Each test compares an enabled run with the logging-disabled counterfactual on
the same repository. The invariant is that the decision, its standard output,
and the exit code are identical: the log is written only where Git cannot
report it, so enabling it can neither create nor hide an untracked change. The
generated property covers pre-existing daily-log states in a state directory
that Git either ignores or sees.
"""

from __future__ import annotations

import datetime as dt
import json
import typing as typ

from hook_support import (
    GeneratedLogState,
    daily_log_states,
    expects_deletion,
    finish_record,
    gate_entry,
    git_run,
    hook_environment,
    hook_record_line,
    hook_stderr,
    is_owned_daily_file,
    prepare_parity_repository,
    read_records,
    run_hook,
    run_hook_in,
    run_with_logging_disabled,
    state_directory_snapshot,
)
from hypothesis import HealthCheck, given, settings

if typ.TYPE_CHECKING:
    from pathlib import Path

VISIBLE_WARNING = "could appear as a worktree change"
OWNERSHIP_WARNING = "was not written by this hook"
WRITTEN_BY_HAND = "written by hand\n"


def _log_dir(state_home: Path) -> Path:
    """Return the decision-log directory beneath ``state_home``."""
    return state_home / "post-turn-quality-stop-hook" / "log"


def _today_daily(state_home: Path) -> Path:
    """Return today's canonical daily file beneath ``state_home``."""
    date = dt.datetime.now(dt.UTC).date().isoformat()
    return _log_dir(state_home) / f"hook-{date}.jsonl"


def test_a_state_directory_outside_the_worktree_logs_normally(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """The default placement is invisible to the gates and always writes."""
    state_home = tmp_path / "state"

    logged = run_hook(repo, tmp_path, state_home, fake_make)
    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == ""
    assert hook_stderr(logged) == "", "an invisible location needs no warning"
    finish = finish_record(read_records(state_home))
    assert finish["decision"] == "allow"
    assert gate_entry(finish, "uncommitted_changes")["outcome"] == "pass"


def test_a_state_directory_inside_the_worktree_writes_nothing(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A visible log location is refused, and the decision is unchanged.

    The daily file the hook would create is itself an untracked change, so
    writing it could block a stop that passes with logging disabled. The hook
    refuses the location instead, warns on standard error, and leaves the
    worktree exactly as Git reports it.
    """
    state_home = repo / "state"

    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)
    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == "", (
        "an unignored state directory inside the worktree must not change the decision"
    )
    assert not state_home.exists(), "the refused location must not be created"
    assert VISIBLE_WARNING in logged.stderr
    assert read_records(state_home) == []


def test_a_git_ignored_state_directory_logs_and_matches_disabled(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """Git-ignoring the log makes it invisible, so it may be written."""
    (repo / ".gitignore").write_text("state/\n", encoding="utf-8")
    git_run(repo, "add", ".gitignore")
    git_run(repo, "commit", "-q", "-m", "ignore the state directory")
    state_home = repo / "state"

    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)
    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == ""
    assert hook_stderr(logged) == "", "an ignored location needs no warning"
    finish = finish_record(read_records(state_home))
    assert finish["decision"] == "allow"
    assert gate_entry(finish, "uncommitted_changes")["outcome"] == "pass"


def test_a_pre_existing_untracked_file_in_the_state_directory_blocks(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """The hook cannot hide repository content, even beside its own log."""
    log_dir = _log_dir(repo / "state")
    log_dir.mkdir(parents=True)
    (log_dir / "leftover.py").write_text("x = 1\n", encoding="utf-8")

    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)
    logged = run_hook(repo, tmp_path, repo / "state", fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout
    assert json.loads(logged.stdout)["decision"] == "block"
    assert VISIBLE_WARNING in logged.stderr
    assert not _today_daily(repo / "state").exists()


def test_a_foreign_daily_file_outside_the_worktree_is_preserved(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A file the hook did not write is never appended to, even off-worktree."""
    state_home = tmp_path / "state"
    daily = _today_daily(state_home)
    daily.parent.mkdir(parents=True)
    daily.write_text(WRITTEN_BY_HAND, encoding="utf-8")

    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)
    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == ""
    assert daily.read_text(encoding="utf-8") == WRITTEN_BY_HAND, (
        "a file the hook cannot prove it wrote must be left untouched"
    )
    assert OWNERSHIP_WARNING in logged.stderr


def test_an_empty_daily_file_outside_the_worktree_is_preserved(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """An empty file proves nothing, so the hook does not adopt it."""
    state_home = tmp_path / "state"
    daily = _today_daily(state_home)
    daily.parent.mkdir(parents=True)
    daily.touch()

    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == 0, logged.stderr
    assert logged.stdout == ""
    assert daily.read_text(encoding="utf-8") == ""
    assert OWNERSHIP_WARNING in logged.stderr


def test_an_owned_daily_file_outside_the_worktree_is_appended(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A file made entirely of hook records is reused, not replaced."""
    state_home = tmp_path / "state"
    daily = _today_daily(state_home)
    daily.parent.mkdir(parents=True)
    earlier = hook_record_line("run-1")
    daily.write_text(earlier, encoding="utf-8")

    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == 0, logged.stderr
    assert logged.stdout == ""
    assert hook_stderr(logged) == "", "reuse is the normal case and warns about nothing"
    assert daily.read_text(encoding="utf-8").startswith(earlier)
    assert finish_record(read_records(state_home))["decision"] == "allow"


def test_a_tracked_daily_log_inside_the_worktree_is_left_alone(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A committed daily file is never modified, so a clean tree stays clean."""
    state_home = repo / "state"
    tracked = _today_daily(state_home)
    tracked.parent.mkdir(parents=True)
    tracked.write_text(WRITTEN_BY_HAND, encoding="utf-8")
    git_run(repo, "add", str(tracked))
    git_run(repo, "commit", "-q", "-m", "commit a daily log file")

    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)
    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == ""
    assert tracked.read_text(encoding="utf-8") == WRITTEN_BY_HAND
    assert "tracked by Git" in logged.stderr


def test_a_modified_tracked_daily_log_still_blocks(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """Refusing the write must not hide a change the user already made."""
    state_home = repo / "state"
    tracked = _today_daily(state_home)
    tracked.parent.mkdir(parents=True)
    tracked.write_text(WRITTEN_BY_HAND, encoding="utf-8")
    git_run(repo, "add", str(tracked))
    git_run(repo, "commit", "-q", "-m", "commit a daily log file")
    tracked.write_text(WRITTEN_BY_HAND + "local edit\n", encoding="utf-8")

    result = run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    assert "tracked by Git" in result.stderr


def test_a_carriage_return_in_the_state_directory_writes_nothing(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """An awkward path is refused like any other visible location."""
    state_home = repo / "state\r"

    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)
    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == ""
    assert not state_home.exists()
    assert VISIBLE_WARNING in logged.stderr


@settings(
    max_examples=8,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(case=daily_log_states())
def test_generated_state_directory_states_keep_the_decision(
    tmp_path: Path, case: GeneratedLogState
) -> None:
    """Any pre-existing daily state leaves the decision identical to no logging.

    The disabled counterfactual runs first on the very same tree, so nothing
    the enabled run does may change what the gates see. When Git sees the state
    directory, the enabled run must not create, change, or delete anything
    there. When Git ignores it, the run may write and may prune only an expired
    file it can prove it wrote.
    """
    repo, fake_make, state_home, daily = prepare_parity_repository(tmp_path, case)
    before = state_directory_snapshot(state_home)
    disabled = run_with_logging_disabled(repo, fake_make.parent, fake_make)
    logged = run_hook_in(
        repo, hook_environment(fake_make.parent, state_home, fake_make)
    )

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout, (
        "the decision and its payload must not depend on logging"
    )
    disabled_decision = (
        json.loads(disabled.stdout)["decision"] if disabled.stdout else "allow"
    )
    visible = case.ignore_rule is None
    assert disabled_decision == ("block" if visible else "allow"), (
        "only content the disabled counterfactual also sees may block"
    )
    if visible:
        assert state_directory_snapshot(state_home) == before, (
            "a visible state directory must not be created, changed, or pruned"
        )
    else:
        assert daily.exists() != expects_deletion(case), (
            "retention may remove an expired file only when Git ignores it"
        )
        if daily.exists() and is_owned_daily_file(case):
            assert daily.read_text(encoding="utf-8").startswith(case.content), (
                "an owned daily file is appended to, never replaced"
            )
        if daily.exists() and not is_owned_daily_file(case):
            assert daily.read_text(encoding="utf-8") == case.content, (
                "a file the hook cannot prove it wrote is left untouched"
            )
