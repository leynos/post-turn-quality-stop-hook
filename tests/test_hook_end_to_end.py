"""End-to-end hook runs that check decisions against persisted records.

These tests run the real hook process in a real Git repository. The quality
command is a small stand-in executable, so no project tooling runs, but the
decision, standard output, exit code, and decision-log records are all
produced by the production path.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
import tempfile
import time
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

if typ.TYPE_CHECKING:
    from pathlib import Path

GIT = shutil.which("git") or pytest.skip(
    "git is required for end-to-end hook tests", allow_module_level=True
)


def _git_env() -> dict[str, str]:
    """Return an isolated Git environment for the test repository."""
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    })
    return env


def _git(repo: Path, *args: str) -> None:
    """Run a Git command in the test repository."""
    subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: fixed Git argv, no shell.
        [GIT, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )


def _make_repository(tmp_path: Path) -> tuple[Path, Path]:
    """Create a clean repository with one committed code change."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    (repo / "Makefile").write_text(
        "check-fmt:\n\t@true\nlint:\n\t@true\ntypecheck:\n\t@true\n",
        encoding="utf-8",
    )
    _git(repo, "add", "README.md", "Makefile")
    _git(repo, "commit", "-q", "-m", "initial")
    (repo / "src").mkdir()
    (repo / "src" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "src/foo.py")
    _git(repo, "commit", "-q", "-m", "change code")
    # Protected branches skip the branch-state gates, so the run must happen on
    # a feature branch for those gates to be exercised.
    _git(repo, "checkout", "-q", "-b", "feature")
    return repo, tmp_path / "fake-make"


def _write_fake_make(path: Path, *, exit_code: int) -> Path:
    """Write an executable that reports the chosen quality result."""
    path.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _hook_environment(
    tmp_path: Path,
    state_home: Path,
    fake_make: Path,
) -> dict[str, str]:
    """Return an isolated environment for one hook process."""
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(state_home),
        "POST_TURN_BASE_REF": "HEAD~1",
        "POST_TURN_BUILD_DRIVER": "make",
        "POST_TURN_MAKE_BIN": str(fake_make),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    })
    env.pop("POST_TURN_LOG", None)
    return env


def _hook_command(*extra_args: str) -> list[str]:
    """Return the argv that runs the real hook module."""
    return [sys.executable, "-m", "post_turn_quality_stop_hook.hook", *extra_args]


def _run_hook_in(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the real hook to completion with a prepared environment."""
    return subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # fixed argv, no shell.
        _hook_command(),
        cwd=repo,
        input=json.dumps({"cwd": str(repo), "session_id": "sess-e2e"}),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )


def _run_hook(
    repo: Path,
    tmp_path: Path,
    state_home: Path,
    fake_make: Path,
) -> subprocess.CompletedProcess[str]:
    """Run the real hook to completion in ``repo``."""
    return _run_hook_in(repo, _hook_environment(tmp_path, state_home, fake_make))


def _start_hook(
    repo: Path,
    tmp_path: Path,
    state_home: Path,
    fake_make: Path,
) -> subprocess.Popen[str]:
    """Start the hook with standard input left open for the caller."""
    return subprocess.Popen(  # ruff:ignore[subprocess-without-shell-equals-true]  # fixed argv, no shell.
        _hook_command(),
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_hook_environment(tmp_path, state_home, fake_make),
    )


def _send_hook_input(process: subprocess.Popen[str], repo: Path) -> None:
    """Write the hook payload and close standard input."""
    assert process.stdin is not None, "the test opened a stdin pipe"
    process.stdin.write(json.dumps({"cwd": str(repo), "session_id": "sess-e2e"}))
    process.stdin.close()


_DAILY_LOG_NAME = re.compile(r"hook-[0-9]{4}-[0-9]{2}-[0-9]{2}\.jsonl")


def _records(state_home: Path) -> list[dict[str, object]]:
    """Read the hook's records, tolerating a log directory not yet made.

    Only ISO-dated daily files are read: the log directory may also hold
    unrelated files, including ones named like a log but without a date.
    """
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    if not log_dir.is_dir():
        return []
    return [
        typ.cast("dict[str, object]", json.loads(line))
        for path in sorted(log_dir.iterdir())
        if _DAILY_LOG_NAME.fullmatch(path.name)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _wait_for_record(
    state_home: Path,
    *,
    record_type: str,
    event: str | None = None,
    timeout: float = 20.0,
) -> bool:
    """Wait until a matching record is persisted, reporting whether it was."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for record in _records(state_home):
            if record.get("record_type") != record_type:
                continue
            if event is None or record.get("event") == event:
                return True
        time.sleep(0.05)
    return False


def _stop(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Wait for the hook to exit, killing it only if it outlives the timeout."""
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    return stdout, stderr


def _finish(records: list[dict[str, object]]) -> dict[str, object]:
    """Return the single finish record."""
    finishes = [record for record in records if record["record_type"] == "finish"]
    assert len(finishes) == 1, f"expected one finish record but got {finishes!r}"
    return finishes[0]


def _gate(finish: dict[str, object], name: str) -> dict[str, object]:
    """Return one gate entry from a finish record."""
    gates = typ.cast("list[dict[str, object]]", finish["gates"])
    return next(gate for gate in gates if gate["gate"] == name)


def test_completed_allow_is_persisted_with_its_finish_record(
    tmp_path: Path,
) -> None:
    """A passing run allows, prints nothing, and persists a matching finish."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = tmp_path / "state"

    result = _run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "", "a successful hook writes nothing to stdout"
    records = _records(state_home)
    start = records[0]
    assert start["record_type"] == "start"
    finish = _finish(records)
    assert finish["session_id"] == "sess-e2e"
    assert finish["decision"] == "allow"
    assert finish["exit_code"] == 0
    assert finish["run_id"] == start["run_id"]
    assert _gate(finish, "quality_checks")["outcome"] == "pass"
    assert _gate(finish, "uncommitted_changes")["outcome"] == "pass"
    assert "startup" in typ.cast("dict[str, object]", finish["stages"])


def test_completed_block_is_persisted_with_its_finish_record(
    tmp_path: Path,
) -> None:
    """A failing quality gate blocks, prints the JSON, and records the block."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=1)
    state_home = tmp_path / "state"

    result = _run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block", payload
    finish = _finish(_records(state_home))
    assert finish["decision"] == "block"
    assert finish["exit_code"] == 0
    assert _gate(finish, "quality_checks")["outcome"] == "block"
    assert _gate(finish, "uncommitted_changes")["outcome"] == "not_reached", (
        "a gate after the blocking quality check must be recorded as unreached"
    )


@pytest.mark.parametrize(
    ("exit_code", "decision"),
    [(0, "allow"), (1, "block")],
    ids=["allow", "block"],
)
def test_unwritable_log_leaves_a_completed_decision_unchanged(
    tmp_path: Path, exit_code: int, decision: str
) -> None:
    """Completed allow and block runs match with an unwritable log."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=exit_code)
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")

    writable = _run_hook(repo, tmp_path, tmp_path / "state", fake_make)
    unwritable = _run_hook(repo, tmp_path, blocker / "state", fake_make)

    assert unwritable.returncode == writable.returncode
    assert unwritable.stdout == writable.stdout
    assert "could not write the decision log" in unwritable.stderr
    assert _finish(_records(tmp_path / "state"))["decision"] == decision


def test_quoted_log_directory_inside_the_worktree_matches_logging_disabled(
    tmp_path: Path,
) -> None:
    """A log directory Git must quote is still excluded from the untracked list."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    # Git C-quotes a path containing a quote, a backslash, or a newline, so a
    # newline-separated query would never match this exclusion prefix.
    state_home = repo / 'state"\\\n'
    state_home.mkdir()

    # The disabled run goes first; it must not see a log directory the enabled
    # run left behind, or the two runs would not be comparable.
    disabled_env = _hook_environment(tmp_path, tmp_path / "unused-state", fake_make)
    disabled_env["POST_TURN_LOG"] = "0"
    disabled = _run_hook_in(repo, disabled_env)
    logged = _run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == ""
    assert _records(state_home), "the enabled run must write its own log"
    finish = _finish(_records(state_home))
    assert finish["decision"] == "allow"
    assert _gate(finish, "uncommitted_changes")["outcome"] == "pass", (
        "the hook's own quoted log path must not count as an untracked change"
    )


def test_disabled_logging_does_not_hide_untracked_log_directory_files(
    tmp_path: Path,
) -> None:
    """A disabled run writes nothing, so it must exclude nothing."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / "leftover.py").write_text("x = 1\n", encoding="utf-8")

    env = _hook_environment(tmp_path, state_home, fake_make)
    env["POST_TURN_LOG"] = "0"
    result = _run_hook_in(repo, env)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block", (
        "a disabled run must not hide an untracked file in the log directory"
    )
    assert _records(state_home) == [], "logging is disabled, so nothing is written"


def test_a_foreign_untracked_daily_file_still_blocks(tmp_path: Path) -> None:
    """A daily name alone does not let the log hide someone else's file.

    The sink refuses to append to a file whose first record it did not write,
    so the file is never excluded and the uncommitted-changes gate reports it
    exactly as it would with logging disabled.
    """
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    today = dt.datetime.now(dt.UTC).date().isoformat()
    foreign = log_dir / f"hook-{today}.jsonl"
    foreign.write_text(
        '{"schema_version": 1, "record_type": "inventory"}\n', encoding="utf-8"
    )

    disabled_env = _hook_environment(tmp_path, tmp_path / "unused-state", fake_make)
    disabled_env["POST_TURN_LOG"] = "0"
    disabled = _run_hook_in(repo, disabled_env)
    logged = _run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout
    assert json.loads(logged.stdout)["decision"] == "block"
    assert foreign.read_text(encoding="utf-8") == (
        '{"schema_version": 1, "record_type": "inventory"}\n'
    )
    assert "was not written by this hook" in logged.stderr


def _hook_record_line(run_id: str = "earlier-run") -> str:
    """Return one JSONL line shaped like a record the hook writes."""
    return (
        json.dumps({
            "schema_version": 1,
            "producer": "post-turn-quality-stop-hook",
            "record_type": "start",
            "run_id": run_id,
            "pid": 1,
            "timestamp": "2026-01-01T00:00:00+00:00",
        })
        + "\n"
    )


def _repository_with_daily_log(
    tmp_path: Path, content: str = ""
) -> tuple[Path, Path, Path, Path]:
    """Build a repository whose state directory holds a pre-created daily log.

    The log lives inside the repository so the uncommitted-changes gate sees it
    as untracked repository content, which is exactly the parity question.
    """
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    today = dt.datetime.now(dt.UTC).date().isoformat()
    daily = log_dir / f"hook-{today}.jsonl"
    daily.write_text(content, encoding="utf-8")
    return repo, fake_make, state_home, daily


def test_an_expired_log_inside_the_worktree_matches_logging_disabled(
    tmp_path: Path,
) -> None:
    """Retention must not delete a file the gates would otherwise report.

    An expired daily log inside the worktree is repository content. A
    logging-disabled run reports it as an untracked change and blocks, so an
    enabled run that pruned it could turn that block into an allow.
    """
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    expired = log_dir / "hook-2020-01-01.jsonl"
    expired.write_text(_hook_record_line(), encoding="utf-8")

    disabled_env = _hook_environment(tmp_path, tmp_path / "unused-state", fake_make)
    disabled_env["POST_TURN_LOG"] = "0"
    disabled = _run_hook_in(repo, disabled_env)
    logged = _run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout
    assert json.loads(logged.stdout)["decision"] == "block"
    assert expired.exists(), "retention must not remove a file inside the worktree"
    assert expired.read_text(encoding="utf-8") == _hook_record_line()


def test_a_pre_existing_empty_daily_file_matches_logging_disabled(
    tmp_path: Path,
) -> None:
    """An empty file this run adopts stays visible to the gates.

    Logging-disabled runs report the untracked file, so the enabled run that
    appends to it must report it too. Only a file the run created may be
    excluded.
    """
    repo, fake_make, state_home, daily = _repository_with_daily_log(tmp_path)

    disabled_env = _hook_environment(tmp_path, tmp_path / "unused-state", fake_make)
    disabled_env["POST_TURN_LOG"] = "0"
    disabled = _run_hook_in(repo, disabled_env)
    logged = _run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout
    assert json.loads(logged.stdout)["decision"] == "block"
    assert daily.read_text(encoding="utf-8").strip() != "", (
        "the run must adopt the empty file and record its start"
    )


def test_a_pre_existing_recognized_daily_file_matches_logging_disabled(
    tmp_path: Path,
) -> None:
    """An earlier run's daily file stays visible to the gates.

    The file was untracked before this run started, so hiding it because the
    sink appends to it would change the decision a disabled run makes.
    """
    first = _hook_record_line("run-1")
    repo, fake_make, state_home, daily = _repository_with_daily_log(tmp_path, first)

    disabled_env = _hook_environment(tmp_path, tmp_path / "unused-state", fake_make)
    disabled_env["POST_TURN_LOG"] = "0"
    disabled = _run_hook_in(repo, disabled_env)
    logged = _run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout
    assert json.loads(logged.stdout)["decision"] == "block"
    assert daily.read_text(encoding="utf-8").startswith(first), (
        "the earlier record must be preserved rather than hidden or replaced"
    )


@dataclasses.dataclass(frozen=True)
class _GeneratedLogState:
    """One generated pre-existing daily-log state for a parity run."""

    content: str
    retention_days: int
    age_days: int
    is_ignored: bool


def _daily_log_states() -> st.SearchStrategy[_GeneratedLogState]:
    """Build a generated daily-log state for the parity property."""
    return st.builds(
        _GeneratedLogState,
        content=st.sampled_from(["", _hook_record_line(), "notes here\n"]),
        retention_days=st.integers(min_value=0, max_value=20),
        age_days=st.integers(min_value=0, max_value=40),
        is_ignored=st.booleans(),
    )


def _prepare_parity_repository(
    tmp_path: Path, case: _GeneratedLogState
) -> tuple[Path, Path, Path, Path]:
    """Create the repository, the pre-existing daily log, and the fake gate.

    Hypothesis reuses the pytest fixture across examples, so every example gets
    its own directory tree and repository.
    """
    base = pathlib.Path(tempfile.mkdtemp(dir=tmp_path))
    repo, fake_path = _make_repository(base)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    if case.is_ignored:
        (repo / ".gitignore").write_text("state/\n", encoding="utf-8")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore the state directory")
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    log_date = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=case.age_days)
    daily = log_dir / f"hook-{log_date.isoformat()}.jsonl"
    daily.write_text(case.content, encoding="utf-8")
    _write_xdg_config(
        base, f"log_enabled = true\nlog_retention_days = {case.retention_days}\n"
    )
    return repo, fake_make, state_home, daily


def _expects_deletion(case: _GeneratedLogState) -> bool:
    """Return whether retention is allowed to remove the generated daily file."""
    return (
        case.is_ignored
        and case.age_days > case.retention_days
        and case.content == _hook_record_line()
    )


def _run_parity_pair(
    tmp_path: Path, repo: Path, state_home: Path, fake_make: Path
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    """Run the hook with logging disabled, then enabled, in one repository."""
    disabled_env = _hook_environment(tmp_path, tmp_path / "unused-state", fake_make)
    disabled_env["POST_TURN_LOG"] = "0"
    disabled = _run_hook_in(repo, disabled_env)
    logged = _run_hook_in(repo, _hook_environment(tmp_path, state_home, fake_make))
    return disabled, logged


@settings(
    max_examples=8,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(case=_daily_log_states())
def test_generated_daily_log_states_match_logging_disabled(
    tmp_path: Path, case: _GeneratedLogState
) -> None:
    """Any pre-existing daily log keeps the decision identical to no logging.

    The generated state covers an empty, recognized and foreign file, any
    retention window, any age up to twice the default window, and a state
    directory Git either sees or ignores. Logging may only ever change the
    repository's visible state when Git itself hides the file, so the enabled
    run must match the disabled one in exit code and standard output, and may
    delete an expired file only when that deletion is invisible to the gates.
    """
    repo, fake_make, state_home, daily = _prepare_parity_repository(tmp_path, case)
    base = fake_make.parent

    disabled, logged = _run_parity_pair(base, repo, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout, (
        "the decision and its payload must not depend on logging"
    )
    disabled_decision = (
        json.loads(disabled.stdout)["decision"] if disabled.stdout else "allow"
    )
    assert disabled_decision == ("allow" if case.is_ignored else "block"), (
        "an ignored daily log must be invisible to both runs"
    )
    assert daily.exists() != _expects_deletion(case), (
        "retention may remove an expired file only when Git ignores it"
    )


def test_a_configuration_opt_out_matches_logging_disabled(tmp_path: Path) -> None:
    """A late opt-out leaves only the records written before it was read.

    The start and input-stage records exist before the configuration is read;
    they are created by this run and so excluded from its own gates. Every
    later record, including the finish, is suppressed. The decision is then
    the same as a run disabled from the start.
    """
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    _write_xdg_config(tmp_path, "log_enabled = false\n")
    state_home = repo / "state"

    disabled_env = _hook_environment(tmp_path, tmp_path / "unused-state", fake_make)
    disabled_env["POST_TURN_LOG"] = "0"
    disabled = _run_hook_in(repo, disabled_env)
    opted_out = _run_hook_in(repo, _hook_environment(tmp_path, state_home, fake_make))

    assert opted_out.returncode == disabled.returncode == 0, opted_out.stderr
    assert opted_out.stdout == disabled.stdout == ""
    records = _records(state_home)
    assert [record["record_type"] for record in records] == [
        "start",
        "progress",
        "progress",
        "progress",
    ]
    assert all(
        record.get("stage") == "input" or record.get("event") == "correlation"
        for record in records[1:]
    ), "the opt-out must suppress every record after the configuration load"
    assert "finish" not in {record["record_type"] for record in records}


def test_carriage_return_in_the_state_directory_matches_logging_disabled(
    tmp_path: Path,
) -> None:
    """A carriage return in the state path must not change the decision."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    # Git's ``-z`` query keeps this raw path; a decode that translated the
    # carriage return into a newline would break the exclusion match.
    state_home = repo / "state\r"
    state_home.mkdir()

    disabled_env = _hook_environment(tmp_path, tmp_path / "unused-state", fake_make)
    disabled_env["POST_TURN_LOG"] = "0"
    disabled = _run_hook_in(repo, disabled_env)
    logged = _run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == ""
    finish = _finish(_records(state_home))
    assert finish["decision"] == "allow"
    assert _gate(finish, "uncommitted_changes")["outcome"] == "pass", (
        "the hook's own log files must not count as repository changes"
    )


@pytest.mark.parametrize("stray_name", ["leftover.py", "hook-abcd-ef-gh.jsonl"])
def test_enabled_logging_still_reports_an_unrelated_log_directory_file(
    tmp_path: Path, stray_name: str
) -> None:
    """The exclusion hides the daily files, not every path beside them."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / stray_name).write_text("x = 1\n", encoding="utf-8")

    # The disabled run goes first so neither run sees the other's log files.
    disabled_env = _hook_environment(tmp_path, tmp_path / "unused-state", fake_make)
    disabled_env["POST_TURN_LOG"] = "0"
    disabled = _run_hook_in(repo, disabled_env)
    logged = _run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout
    assert json.loads(logged.stdout)["decision"] == "block", (
        "an unrelated file in the log directory must stay visible"
    )
    finish = _finish(_records(state_home))
    assert _gate(finish, "uncommitted_changes")["outcome"] == "block"


def test_tracked_change_under_the_log_directory_still_blocks(
    tmp_path: Path,
) -> None:
    """The exclusion hides untracked log files only, never a tracked change."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = repo / 'state"\\\n'
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    tracked = log_dir / "tracked.py"
    tracked.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", str(tracked))
    _git(repo, "commit", "-q", "-m", "add tracked file under the log directory")
    tracked.write_text("x = 2\n", encoding="utf-8")

    result = _run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    finish = _finish(_records(state_home))
    assert _gate(finish, "uncommitted_changes")["outcome"] == "block", (
        "a tracked file under the excluded directory is still a repository change"
    )


def test_a_tracked_daily_log_does_not_change_the_decision(tmp_path: Path) -> None:
    """A committed daily log is left alone, so the gate sees a clean tree."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    today = dt.datetime.now(dt.UTC).date().isoformat()
    tracked = log_dir / f"hook-{today}.jsonl"
    tracked.write_text("committed placeholder\n", encoding="utf-8")
    _git(repo, "add", str(tracked))
    _git(repo, "commit", "-q", "-m", "commit a daily log file")

    # The disabled run goes first so neither run sees the other's log files.
    disabled_env = _hook_environment(tmp_path, tmp_path / "unused-state", fake_make)
    disabled_env["POST_TURN_LOG"] = "0"
    disabled = _run_hook_in(repo, disabled_env)
    logged = _run_hook(repo, tmp_path, state_home, fake_make)

    assert logged.returncode == disabled.returncode == 0, logged.stderr
    assert logged.stdout == disabled.stdout == ""
    assert tracked.read_text(encoding="utf-8") == "committed placeholder\n", (
        "the hook must not append to a tracked daily log"
    )
    assert "tracked by Git" in logged.stderr


def test_a_modified_tracked_daily_log_still_blocks(tmp_path: Path) -> None:
    """Refusing the write must not hide a pre-existing tracked change."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    today = dt.datetime.now(dt.UTC).date().isoformat()
    tracked = log_dir / f"hook-{today}.jsonl"
    tracked.write_text("committed placeholder\n", encoding="utf-8")
    _git(repo, "add", str(tracked))
    _git(repo, "commit", "-q", "-m", "commit a daily log file")
    tracked.write_text("committed placeholder\nlocal edit\n", encoding="utf-8")

    result = _run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block", (
        "a tracked daily log already modified by the user is still a change"
    )


def test_untracked_sibling_of_the_log_directory_still_blocks(
    tmp_path: Path,
) -> None:
    """A prefix match must not hide an untracked directory beside the log."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = repo / 'state"\\\n'
    state_home.mkdir()
    sibling = state_home.parent / f"{state_home.name}-sibling"
    sibling.mkdir()
    (sibling / "app.py").write_text("x = 1\n", encoding="utf-8")

    result = _run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    finish = _finish(_records(state_home))
    assert _gate(finish, "uncommitted_changes")["outcome"] == "block", (
        "an untracked sibling of the log directory must stay visible"
    )


def test_untracked_log_sibling_inside_the_state_directory_still_blocks(
    tmp_path: Path,
) -> None:
    """Keep an untracked sibling of the excluded log directory visible."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = repo / 'state"\\\n'
    sibling = state_home / "post-turn-quality-stop-hook" / "log-sibling"
    sibling.mkdir(parents=True)
    (sibling / "app.py").write_text("x = 1\n", encoding="utf-8")

    result = _run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    finish = _finish(_records(state_home))
    assert _gate(finish, "uncommitted_changes")["outcome"] == "block", (
        "the exclusion is the log directory itself, not every path with that prefix"
    )


@pytest.mark.parametrize("exit_code", [0, 1])
def test_finish_record_joins_correlation_fields(tmp_path: Path, exit_code: int) -> None:
    """Both outcomes keep the same run identity in every record."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=exit_code)
    state_home = tmp_path / "state"

    _run_hook(repo, tmp_path, state_home, fake_make)

    records = _records(state_home)
    run_ids = {record["run_id"] for record in records}
    assert len(run_ids) == 1, f"expected one run id but got {run_ids!r}"
    start = records[0]
    assert start["record_type"] == "start"
    assert "session_id" not in start, (
        "the start record is written before the hook input is read"
    )
    known = next(
        index
        for index, record in enumerate(records)
        if record.get("event") == "correlation"
    )
    assert records[known]["session_id"] == "sess-e2e"
    assert all(record["session_id"] == "sess-e2e" for record in records[known:])


def _write_xdg_config(tmp_path: Path, body: str) -> None:
    """Write an XDG configuration file for the hook process."""
    path = tmp_path / "config" / "post-turn-quality-stop-hook" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _fetch_commands(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return the fetch commands recorded by a completed run."""
    commands = typ.cast("list[dict[str, object]]", _finish(records)["commands"])
    return [command for command in commands if command["kind"] == "fetch"]


def test_real_fetch_is_recorded_with_its_exit_code(tmp_path: Path) -> None:
    """A real fetch from a local remote leaves progress and a finish command."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    origin = tmp_path / "origin.git"
    _git(repo, "init", "--bare", "-q", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    _write_xdg_config(tmp_path, "gate_pr_rebase = false\n")
    state_home = tmp_path / "state"
    env = _hook_environment(tmp_path, state_home, fake_make)
    env.update({
        "POST_TURN_ALWAYS_FETCH": "1",
        "POST_TURN_BASE_REF": "origin/main",
    })

    result = _run_hook_in(repo, env)

    assert result.returncode == 0, result.stderr
    records = _records(state_home)
    starts = [
        record
        for record in records
        if record.get("event") == "command_start"
        and typ.cast("dict[str, object]", record["command"])["kind"] == "fetch"
    ]
    assert len(starts) == 1, f"expected one recorded fetch start but got {starts!r}"
    assert [command["exit_code"] for command in _fetch_commands(records)] == [0]
    assert _finish(records)["decision"] == "allow"


def test_failed_real_fetch_is_recorded_with_its_exit_code(tmp_path: Path) -> None:
    """A missing remote blocks and records the fetch's non-zero exit code."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    _git(repo, "remote", "add", "origin", str(tmp_path / "missing.git"))
    _write_xdg_config(tmp_path, "gate_pr_rebase = false\n")
    state_home = tmp_path / "state"
    env = _hook_environment(tmp_path, state_home, fake_make)
    env.update({
        "POST_TURN_ALWAYS_FETCH": "1",
        "POST_TURN_BASE_REF": "origin/main",
    })

    result = _run_hook_in(repo, env)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    records = _records(state_home)
    fetch_commands = _fetch_commands(records)
    assert len(fetch_commands) == 1, f"expected one fetch but got {fetch_commands!r}"
    assert fetch_commands[0]["exit_code"] != 0, "a failed fetch must record its status"
    assert _finish(records)["decision"] == "block"


def test_run_without_a_fetch_records_no_fetch_command(tmp_path: Path) -> None:
    """A local base ref runs no fetch, so none is recorded."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = tmp_path / "state"
    env = _hook_environment(tmp_path, state_home, fake_make)
    env["POST_TURN_ALWAYS_FETCH"] = "0"

    result = _run_hook_in(repo, env)

    assert result.returncode == 0, result.stderr
    assert _fetch_commands(_records(state_home)) == []


def _write_slow_make(path: Path, marker: Path) -> Path:
    """Write an executable that announces itself, then outlives the signal."""
    path.write_text(f"#!/bin/sh\ntouch {marker}\nsleep 20\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _open_fifo_writer(path: Path, timeout: float = 20.0) -> int:
    """Open ``path`` for writing once a reader is waiting, or fail the test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            time.sleep(0.05)
    pytest.fail("the hook never opened the stalled configuration file")


def _wait_for_file(path: Path, timeout: float = 20.0) -> bool:
    """Wait until ``path`` exists, reporting whether it appeared."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def test_start_record_precedes_a_stalled_input_read(tmp_path: Path) -> None:
    """The log exists before standard input is read, so a stall is visible."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = tmp_path / "state"
    process = _start_hook(repo, tmp_path, state_home, fake_make)
    try:
        assert _wait_for_record(state_home, record_type="start"), (
            "the start record must be written before the hook reads stdin"
        )
        _send_hook_input(process, repo)
        stdout, stderr = _stop(process)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hung hook
            process.kill()
            process.communicate()

    assert process.returncode == 0, stderr
    assert stdout == "", "a successful hook writes nothing to stdout"
    assert _finish(_records(state_home))["decision"] == "allow"


def test_sigterm_while_the_configuration_read_is_stalled_records_interruption(
    tmp_path: Path,
) -> None:
    """The signal guard covers the configuration read, not only the pipeline."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    state_home = tmp_path / "state"
    config_fifo = tmp_path / "stalled-config.toml"
    os.mkfifo(config_fifo)
    process = subprocess.Popen(  # ruff:ignore[subprocess-without-shell-equals-true]  # fixed argv, no shell.
        _hook_command("--config", str(config_fifo)),
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_hook_environment(tmp_path, state_home, fake_make),
    )
    writer: int | None = None
    try:
        _send_hook_input(process, repo)
        # A non-blocking writer open succeeds only once the hook has opened the
        # FIFO for reading, so this pins the signal to the stalled config read.
        writer = _open_fifo_writer(config_fifo)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = _stop(process)
    finally:
        if writer is not None:
            os.close(writer)
        if process.poll() is None:  # pragma: no cover - only on a hung hook
            process.kill()
            process.communicate()

    assert process.returncode == -signal.SIGTERM, stderr
    assert stdout == "", "an interrupted hook must not print a decision"
    finish = _finish(_records(state_home))
    assert finish["decision"] == "interrupted"
    assert finish["signal"] == "SIGTERM"
    assert finish["exit_code"] == 128 + signal.SIGTERM


def test_sigterm_during_a_command_records_interruption(tmp_path: Path) -> None:
    """SIGTERM while a build command runs leaves an interrupted finish record."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_slow_make(fake_path, tmp_path / "make-started")
    state_home = tmp_path / "state"
    process = _start_hook(repo, tmp_path, state_home, fake_make)
    try:
        _send_hook_input(process, repo)
        assert _wait_for_record(
            state_home, record_type="progress", event="command_start"
        ), "the build command must start before the signal is sent"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = _stop(process)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hung hook
            process.kill()
            process.communicate()

    assert process.returncode == -signal.SIGTERM, stderr
    assert stdout == "", "an interrupted hook must not print a decision"
    finish = _finish(_records(state_home))
    assert finish["decision"] == "interrupted"
    assert finish["signal"] == "SIGTERM"
    assert finish["exit_code"] == 128 + signal.SIGTERM


def test_sigkill_during_a_command_leaves_evidence_without_a_finish(
    tmp_path: Path,
) -> None:
    """SIGKILL cannot write a finish record, so the start and command remain."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_slow_make(fake_path, tmp_path / "make-started")
    state_home = tmp_path / "state"
    process = _start_hook(repo, tmp_path, state_home, fake_make)
    try:
        _send_hook_input(process, repo)
        assert _wait_for_record(
            state_home, record_type="progress", event="command_start"
        ), "the build command must start before the signal is sent"
        process.kill()
        stdout, _stderr = _stop(process)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hung hook
            process.kill()
            process.communicate()

    assert process.returncode == -signal.SIGKILL
    assert stdout == "", "a killed hook must not print a decision"
    records = _records(state_home)
    assert any(record["record_type"] == "start" for record in records)
    assert any(record.get("event") == "command_start" for record in records), (
        "progress must be durable before the protected command finishes"
    )
    assert not any(record["record_type"] == "finish" for record in records), (
        "SIGKILL cannot be caught, so no finish record may appear"
    )


def test_unwritable_log_leaves_a_signal_death_unchanged(tmp_path: Path) -> None:
    """A blocking log location does not change how a signalled hook exits."""
    repo, fake_path = _make_repository(tmp_path)
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")

    def signalled(name: str, state_home: Path) -> tuple[int, str]:
        """Run the hook, signal it mid-build, and return its exit and stdout."""
        marker = tmp_path / f"make-started-{name}"
        fake_make = _write_slow_make(fake_path, marker)
        process = _start_hook(repo, tmp_path, state_home, fake_make)
        _send_hook_input(process, repo)
        # The unwritable run has no log to poll, so wait for the build child
        # itself. Both runs must be signalled at the same point.
        started = _wait_for_file(marker)
        process.send_signal(signal.SIGTERM)
        stdout, _stderr = _stop(process)
        assert started, "the build command must start before the signal is sent"
        return process.returncode, stdout

    writable_code, writable_stdout = signalled("writable", tmp_path / "state")
    unwritable_code, unwritable_stdout = signalled("unwritable", blocker / "state")

    assert unwritable_code == writable_code == -signal.SIGTERM
    assert unwritable_stdout == writable_stdout == ""
