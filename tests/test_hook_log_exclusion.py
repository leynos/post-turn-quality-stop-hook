"""End-to-end parity tests for excluding the hook's own log files.

Each test compares an enabled run with the logging-disabled counterfactual and
checks that the exclusion hides only files the hook owns. The generated
property covers the same relation across pre-existing daily-log states.
"""

from __future__ import annotations

import datetime as dt
import json
import typing as typ

import pytest
from hook_support import (
    GeneratedLogState,
    daily_log_states,
    expects_deletion,
    finish_record,
    gate_entry,
    git_run,
    hook_environment,
    hook_record_line,
    is_owned_daily_file,
    prepare_disabled_counterfactual,
    prepare_parity_repository,
    read_records,
    run_disabled_case,
    run_hook,
    run_hook_in,
    run_with_logging_disabled,
)
from hypothesis import HealthCheck, given, settings

if typ.TYPE_CHECKING:
    from pathlib import Path


def write_daily_file(state_home: Path, content: str) -> Path:
    """Create the log directory and today's daily file under ``state_home``."""
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    daily = log_dir / f"hook-{dt.datetime.now(dt.UTC).date().isoformat()}.jsonl"
    daily.write_text(content, encoding="utf-8")
    return daily


def test_quoted_log_directory_inside_the_worktree_matches_logging_disabled(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A log directory Git must quote is still excluded from the untracked list."""
    # Git C-quotes a path containing a quote, a backslash, or a newline, so a
    # newline-separated query would never match this exclusion prefix.
    state_home = repo / 'state"\\\n'
    state_home.mkdir()

    # The disabled run goes first; it must not see a log directory the enabled
    # run left behind, or the two runs would not be comparable.
    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)
    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == ""
    assert read_records(state_home), "the enabled run must write its own log"
    finish = finish_record(read_records(state_home))
    assert finish["decision"] == "allow"
    assert gate_entry(finish, "uncommitted_changes")["outcome"] == "pass", (
        "the hook's own quoted log path must not count as an untracked change"
    )


def test_disabled_logging_does_not_hide_untracked_log_directory_files(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A disabled run writes nothing, so it must exclude nothing."""
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / "leftover.py").write_text("x = 1\n", encoding="utf-8")

    env = hook_environment(tmp_path, state_home, fake_make)
    env["POST_TURN_LOG"] = "0"
    result = run_hook_in(repo, env)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block", (
        "a disabled run must not hide an untracked file in the log directory"
    )
    assert read_records(state_home) == [], "logging is disabled, so nothing is written"


def test_a_foreign_untracked_daily_file_still_blocks(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A daily name alone does not let the log hide someone else's file.

    The sink refuses to append to a file whose first record it did not write,
    so the file is never excluded and the uncommitted-changes gate reports it
    exactly as it would with logging disabled.
    """
    state_home = repo / "state"
    foreign = write_daily_file(
        state_home, '{"schema_version": 1, "record_type": "inventory"}\n'
    )

    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)
    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout
    assert json.loads(logged.stdout)["decision"] == "block"
    assert foreign.read_text(encoding="utf-8") == (
        '{"schema_version": 1, "record_type": "inventory"}\n'
    )
    assert "was not written by this hook" in logged.stderr


def test_an_expired_log_inside_the_worktree_is_left_but_excluded(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """Retention keeps an expired verified file, and the gates still ignore it.

    The file exists only because logging was enabled, so the counterfactual is
    a repository without it. Deleting it would turn the run's own evidence into
    a deletion, so retention leaves it on disk; excluding it keeps that
    evidence out of the gates. A logging-disabled run handed the same file has
    no recorder to claim it and reports it as untracked, which is why the
    counterfactual is a fresh repository rather than the same one.
    """
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    expired = log_dir / "hook-2020-01-01.jsonl"
    expired.write_text(hook_record_line(), encoding="utf-8")

    logged = run_hook(repo, tmp_path, state_home, fake_make)
    disabled = run_disabled_case(tmp_path / "disabled")

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == ""
    finish = finish_record(read_records(state_home))
    assert finish["decision"] == "allow"
    assert gate_entry(finish, "uncommitted_changes")["outcome"] == "pass", (
        "the hook's own expired file must not count as a repository change"
    )
    assert expired.exists(), "retention must not remove a file inside the worktree"
    assert expired.read_text(encoding="utf-8") == hook_record_line()


def test_a_pre_existing_hook_record_does_not_block_a_later_stop(
    repo: Path, fake_make: Path, tmp_path: Path
) -> None:
    """A daily file holding an earlier run's record never changes the decision.

    A logging-disabled run never creates a daily file, so the counterfactual
    for an enabled run is a repository without one. A file made entirely of
    hook records is verifiably the hook's own, so the enabled run must make the
    same decision as that counterfactual; otherwise enabling logging would
    block every later stop for the rest of the day.
    """
    state_home = repo / "state"
    earlier = hook_record_line("run-1")
    daily = write_daily_file(state_home, earlier)

    logged = run_hook(repo, tmp_path, state_home, fake_make)

    disabled = run_disabled_case(tmp_path / "disabled")

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == "", (
        "the hook's own daily file must not count as an untracked change"
    )
    finish = finish_record(read_records(state_home))
    assert finish["decision"] == "allow"
    assert gate_entry(finish, "uncommitted_changes")["outcome"] == "pass"
    assert daily.read_text(encoding="utf-8").startswith(earlier), (
        "the earlier record must be preserved rather than hidden or replaced"
    )


def test_an_earlier_daily_file_does_not_block_a_later_stop(
    repo: Path, fake_make: Path, tmp_path: Path
) -> None:
    """A previous day's verified log must not become today's untracked change.

    A state directory inside a worktree is reused across days. The second day's
    run claims only its own file, so without excluding earlier files the hook
    can verify as its own, every day after the first would block a stop that a
    logging-disabled run on the same repository allows.
    """
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    earlier = log_dir / f"hook-{yesterday.isoformat()}.jsonl"
    earlier.write_text(hook_record_line("earlier-run"), encoding="utf-8")

    logged = run_hook(repo, tmp_path, state_home, fake_make)
    disabled = run_disabled_case(tmp_path / "disabled")

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == ""
    finish = finish_record(read_records(state_home))
    assert finish["decision"] == "allow"
    assert gate_entry(finish, "uncommitted_changes")["outcome"] == "pass", (
        "yesterday's verified log must be excluded, not reported as a change"
    )
    assert earlier.read_text(encoding="utf-8") == hook_record_line("earlier-run"), (
        "the run must leave the earlier file untouched"
    )


def test_an_earlier_foreign_daily_file_still_blocks(
    repo: Path, fake_make: Path, tmp_path: Path
) -> None:
    """Only a file the hook can prove it wrote is hidden, whatever its date."""
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    earlier = log_dir / f"hook-{yesterday.isoformat()}.jsonl"
    content = '{"schema_version": 1, "record_type": "inventory"}\n'
    earlier.write_text(content, encoding="utf-8")

    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)
    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout
    assert json.loads(logged.stdout)["decision"] == "block", (
        "an earlier file the hook did not write must stay visible"
    )
    assert earlier.read_text(encoding="utf-8") == content


def test_a_disabled_run_sees_a_recognised_log_as_untracked(
    repo: Path, fake_make: Path, tmp_path: Path
) -> None:
    """Without a recorder, a recognisable daily file is ordinary repository data.

    An enabled run reuses and excludes a file it can verify as its own, because
    such a file exists only because logging was enabled. The same file handed to
    a logging-disabled run has no recorder to claim it, so the gate reports it
    as untracked. The divergence is deliberate and is why the counterfactual for
    an enabled run's own log is a repository without it.
    """
    state_home = repo / "state"
    write_daily_file(state_home, hook_record_line("run-1"))

    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)

    assert disabled.returncode == 0, disabled.stderr
    assert json.loads(disabled.stdout)["decision"] == "block"


def test_an_empty_pre_existing_daily_file_matches_logging_disabled(
    repo: Path, fake_make: Path, tmp_path: Path
) -> None:
    """An empty file cannot be shown to be the hook's own, so nothing is written.

    An earlier crash that wrote no line and a file someone else created at the
    same path are indistinguishable. Adopting the file would hide it from the
    gates, so both the append and the exclusion are refused, and the enabled
    run must reach the same decision as a disabled run looking at the same
    untracked file.
    """
    state_home = repo / "state"
    daily = write_daily_file(state_home, "")

    # The disabled run goes first so neither run sees the other's log files.
    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)
    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout
    assert json.loads(logged.stdout)["decision"] == "block"
    assert daily.read_text(encoding="utf-8") == "", (
        "the run must not adopt a file it cannot verify as its own"
    )
    assert "was not written by this hook" in logged.stderr
    assert read_records(state_home) == [], (
        "refusing the daily file leaves nowhere to record the run"
    )


@settings(
    max_examples=8,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(case=daily_log_states())
def test_generated_daily_log_states_match_logging_disabled(
    tmp_path: Path, case: GeneratedLogState
) -> None:
    """Any pre-existing daily log keeps the decision identical to no logging.

    The generated state covers an empty, recognized and foreign file, any
    retention window, any age up to twice the default window, and a state
    directory Git either sees or ignores. The disabled counterfactual runs in
    an otherwise identical repository without a file this logging run would
    have owned, because logging-disabled runs never create one. Logging may
    only change the repository's visible state when Git itself hides the file,
    so the enabled run must match the disabled one in exit code and standard
    output, and may delete an expired file only when that deletion is
    invisible to the gates.
    """
    repo, fake_make, state_home, daily = prepare_parity_repository(tmp_path, case)
    disabled_repo, disabled_make = prepare_disabled_counterfactual(tmp_path, case)

    disabled = run_with_logging_disabled(
        disabled_repo, disabled_make.parent, disabled_make
    )
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
    visible = not case.is_ignored and not is_owned_daily_file(case)
    assert disabled_decision == ("block" if visible else "allow"), (
        "only content the disabled counterfactual also sees may block"
    )
    assert daily.exists() != expects_deletion(case), (
        "retention may remove an expired file only when Git ignores it"
    )


def test_carriage_return_in_the_state_directory_matches_logging_disabled(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A carriage return in the state path must not change the decision."""
    # Git's ``-z`` query keeps this raw path; a decode that translated the
    # carriage return into a newline would break the exclusion match.
    state_home = repo / "state\r"
    state_home.mkdir()

    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)
    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == ""
    finish = finish_record(read_records(state_home))
    assert finish["decision"] == "allow"
    assert gate_entry(finish, "uncommitted_changes")["outcome"] == "pass", (
        "the hook's own log files must not count as repository changes"
    )


@pytest.mark.parametrize("stray_name", ["leftover.py", "hook-abcd-ef-gh.jsonl"])
def test_enabled_logging_still_reports_an_unrelated_log_directory_file(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
    stray_name: str,
) -> None:
    """The exclusion hides the daily files, not every path beside them."""
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / stray_name).write_text("x = 1\n", encoding="utf-8")

    # The disabled run goes first so neither run sees the other's log files.
    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)
    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout
    assert json.loads(logged.stdout)["decision"] == "block", (
        "an unrelated file in the log directory must stay visible"
    )
    finish = finish_record(read_records(state_home))
    assert gate_entry(finish, "uncommitted_changes")["outcome"] == "block"


def test_tracked_change_under_the_log_directory_still_blocks(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """The exclusion hides untracked log files only, never a tracked change."""
    state_home = repo / 'state"\\\n'
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    tracked = log_dir / "tracked.py"
    tracked.write_text("x = 1\n", encoding="utf-8")
    git_run(repo, "add", str(tracked))
    git_run(repo, "commit", "-q", "-m", "add tracked file under the log directory")
    tracked.write_text("x = 2\n", encoding="utf-8")

    result = run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    finish = finish_record(read_records(state_home))
    assert gate_entry(finish, "uncommitted_changes")["outcome"] == "block", (
        "a tracked file under the excluded directory is still a repository change"
    )


def test_a_tracked_daily_log_does_not_change_the_decision(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A committed daily log is left alone, so the gate sees a clean tree."""
    state_home = repo / "state"
    tracked = write_daily_file(state_home, "committed placeholder\n")
    git_run(repo, "add", str(tracked))
    git_run(repo, "commit", "-q", "-m", "commit a daily log file")

    # The disabled run goes first so neither run sees the other's log files.
    disabled = run_with_logging_disabled(repo, tmp_path, fake_make)
    logged = run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == ""
    assert tracked.read_text(encoding="utf-8") == "committed placeholder\n", (
        "the hook must not append to a tracked daily log"
    )
    assert "tracked by Git" in logged.stderr


def test_a_modified_tracked_daily_log_still_blocks(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """Refusing the write must not hide a pre-existing tracked change."""
    state_home = repo / "state"
    tracked = write_daily_file(state_home, "committed placeholder\n")
    git_run(repo, "add", str(tracked))
    git_run(repo, "commit", "-q", "-m", "commit a daily log file")
    tracked.write_text("committed placeholder\nlocal edit\n", encoding="utf-8")

    result = run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block", (
        "a tracked daily log already modified by the user is still a change"
    )


def test_untracked_sibling_of_the_log_directory_still_blocks(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A prefix match must not hide an untracked directory beside the log."""
    state_home = repo / 'state"\\\n'
    state_home.mkdir()
    sibling = state_home.parent / f"{state_home.name}-sibling"
    sibling.mkdir()
    (sibling / "app.py").write_text("x = 1\n", encoding="utf-8")

    result = run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    finish = finish_record(read_records(state_home))
    assert gate_entry(finish, "uncommitted_changes")["outcome"] == "block", (
        "an untracked sibling of the log directory must stay visible"
    )


def test_untracked_log_sibling_inside_the_state_directory_still_blocks(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """Keep an untracked sibling of the excluded log directory visible."""
    state_home = repo / 'state"\\\n'
    sibling = state_home / "post-turn-quality-stop-hook" / "log-sibling"
    sibling.mkdir(parents=True)
    (sibling / "app.py").write_text("x = 1\n", encoding="utf-8")

    result = run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    finish = finish_record(read_records(state_home))
    assert gate_entry(finish, "uncommitted_changes")["outcome"] == "block", (
        "the exclusion is the log directory itself, not every path with that prefix"
    )
