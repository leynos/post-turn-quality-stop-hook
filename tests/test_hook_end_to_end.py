"""End-to-end hook runs that check decisions against persisted records.

These tests run the real hook process in a real Git repository. The quality
command is a small stand-in executable, so no project tooling runs, but the
decision, standard output, exit code, and decision-log records are all
produced by the production path.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
import time
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    from pathlib import Path

GIT = "/usr/bin/git"


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
    repo: Path,
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


def _hook_command() -> list[str]:
    """Return the argv that runs the real hook module."""
    return [sys.executable, "-m", "post_turn_quality_stop_hook.hook"]


def _run_hook(
    repo: Path,
    tmp_path: Path,
    state_home: Path,
    fake_make: Path,
) -> subprocess.CompletedProcess[str]:
    """Run the real hook to completion in ``repo``."""
    return subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # fixed argv, no shell.
        _hook_command(),
        cwd=repo,
        input=json.dumps({"cwd": str(repo), "session_id": "sess-e2e"}),
        capture_output=True,
        text=True,
        env=_hook_environment(repo, tmp_path, state_home, fake_make),
        check=False,
        timeout=120,
    )


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
        env=_hook_environment(repo, tmp_path, state_home, fake_make),
    )


def _send_hook_input(process: subprocess.Popen[str], repo: Path) -> None:
    """Write the hook payload and close standard input."""
    assert process.stdin is not None, "the test opened a stdin pipe"
    process.stdin.write(json.dumps({"cwd": str(repo), "session_id": "sess-e2e"}))
    process.stdin.close()


def _records(state_home: Path) -> list[dict[str, object]]:
    """Read whatever records exist, tolerating a log directory not yet made."""
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    if not log_dir.is_dir():
        return []
    return [
        typ.cast("dict[str, object]", json.loads(line))
        for path in sorted(log_dir.glob("hook-*.jsonl"))
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


def test_unwritable_log_leaves_a_completed_decision_unchanged(
    tmp_path: Path,
) -> None:
    """A completed run with an unwritable log matches the writable decision."""
    repo, fake_path = _make_repository(tmp_path)
    fake_make = _write_fake_make(fake_path, exit_code=0)
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")

    writable = _run_hook(repo, tmp_path, tmp_path / "state", fake_make)
    unwritable = _run_hook(repo, tmp_path, blocker / "state", fake_make)

    assert unwritable.returncode == writable.returncode
    assert unwritable.stdout == writable.stdout
    assert "could not write the decision log" in unwritable.stderr
    assert _finish(_records(tmp_path / "state"))["decision"] == "allow"


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


def _write_slow_make(path: Path, marker: Path) -> Path:
    """Write an executable that announces itself, then outlives the signal."""
    path.write_text(f"#!/bin/sh\ntouch {marker}\nsleep 20\n", encoding="utf-8")
    path.chmod(0o755)
    return path


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
