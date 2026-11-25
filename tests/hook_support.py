"""Shared setup for end-to-end hook tests.

These tests run the real hook process in a real Git repository. The quality
command is a small stand-in executable, so no project tooling runs, but the
decision, standard output, exit code, and decision-log records are all
produced by the production path.

The helpers live here so the lifecycle, parity, command, and interruption
modules can share one repository builder and one record reader.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
import tempfile
import time
import typing as typ

import pytest
from hypothesis import strategies as st

if typ.TYPE_CHECKING:
    from pathlib import Path

GIT = shutil.which("git")
_DAILY_LOG_NAME = re.compile(r"hook-[0-9]{4}-[0-9]{2}-[0-9]{2}\.jsonl")


def _require_git() -> str:
    """Return the Git executable, skipping the test when it is unavailable."""
    if GIT is None:  # pragma: no cover - depends on the host installation
        pytest.skip("git is required for end-to-end hook tests")
    return GIT


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


def git_run(repo: Path, *args: str) -> None:
    """Run a Git command in the test repository."""
    subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: fixed Git argv, no shell.
        [_require_git(), *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )


def make_repository(base: Path) -> Path:
    """Create a clean repository with one committed code change."""
    repo = base / "repo"
    repo.mkdir(parents=True)
    git_run(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    (repo / "Makefile").write_text(
        "check-fmt:\n\t@true\nlint:\n\t@true\ntypecheck:\n\t@true\n",
        encoding="utf-8",
    )
    git_run(repo, "add", "README.md", "Makefile")
    git_run(repo, "commit", "-q", "-m", "initial")
    (repo / "src").mkdir()
    (repo / "src" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    git_run(repo, "add", "src/foo.py")
    git_run(repo, "commit", "-q", "-m", "change code")
    # Protected branches skip the branch-state gates, so the run must happen on
    # a feature branch for those gates to be exercised.
    git_run(repo, "checkout", "-q", "-b", "feature")
    return repo


def write_fake_make(path: Path, *, exit_code: int) -> Path:
    """Write an executable that reports the chosen quality result."""
    path.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def hook_environment(
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


def hook_command(*extra_args: str) -> list[str]:
    """Return the argv that runs the real hook module."""
    return [sys.executable, "-m", "post_turn_quality_stop_hook.hook", *extra_args]


def run_hook_in(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the real hook to completion with a prepared environment."""
    return subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # fixed argv, no shell.
        hook_command(),
        cwd=repo,
        input=json.dumps({"cwd": str(repo), "session_id": "sess-e2e"}),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )


_RUNPY_WARNING = "found in sys.modules after import of package"


def hook_stderr(result: subprocess.CompletedProcess[str]) -> str:
    """Return the hook's own standard error for ``result``.

    Running the hook with ``python -m`` re-executes a module the package
    ``__init__`` already imported, so runpy warns on every run. That line
    belongs to the harness rather than the hook, and removing it lets a test
    assert that the hook itself said nothing.
    """
    return "".join(
        line
        for line in result.stderr.splitlines(keepends=True)
        if _RUNPY_WARNING not in line
    )


def run_hook(
    repo: Path,
    tmp_path: Path,
    state_home: Path,
    fake_make: Path,
) -> subprocess.CompletedProcess[str]:
    """Run the real hook to completion in ``repo``."""
    return run_hook_in(repo, hook_environment(tmp_path, state_home, fake_make))


def start_hook(
    repo: Path,
    tmp_path: Path,
    state_home: Path,
    fake_make: Path,
) -> subprocess.Popen[str]:
    """Start the hook with standard input left open for the caller."""
    return subprocess.Popen(  # ruff:ignore[subprocess-without-shell-equals-true]  # fixed argv, no shell.
        hook_command(),
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=hook_environment(tmp_path, state_home, fake_make),
    )


def send_hook_input(process: subprocess.Popen[str], repo: Path) -> None:
    """Write the hook payload and close standard input."""
    if process.stdin is None:  # pragma: no cover - start_hook always opens it
        pytest.fail("the test must start the hook with a stdin pipe")
    process.stdin.write(json.dumps({"cwd": str(repo), "session_id": "sess-e2e"}))
    process.stdin.close()
    # ``Popen.communicate`` flushes stdin when it is still set. Clearing the
    # attribute after closing records that the pipe is finished, so a later
    # ``communicate`` does not try to flush a closed file.
    process.stdin = None


def read_records(state_home: Path) -> list[dict[str, object]]:
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


def wait_for_record(
    state_home: Path,
    *,
    record_type: str,
    event: str | None = None,
    timeout: float = 20.0,
) -> bool:
    """Wait until a matching record is persisted, reporting whether it was."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for record in read_records(state_home):
            if record.get("record_type") != record_type:
                continue
            if event is None or record.get("event") == event:
                return True
        time.sleep(0.05)
    return False


def stop(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Wait for the hook to exit, killing it only if it outlives the timeout."""
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    return stdout, stderr


def finish_record(records: list[dict[str, object]]) -> dict[str, object]:
    """Return the single finish record."""
    finishes = [record for record in records if record["record_type"] == "finish"]
    if len(finishes) != 1:
        pytest.fail(f"expected one finish record but got {finishes!r}")
    return finishes[0]


def gate_entry(finish: dict[str, object], name: str) -> dict[str, object]:
    """Return one gate entry from a finish record."""
    gates = typ.cast("list[dict[str, object]]", finish["gates"])
    return next(gate for gate in gates if gate["gate"] == name)


def hook_record_line(run_id: str = "earlier-run") -> str:
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


def make_case_repository(base: Path) -> tuple[Path, Path, Path]:
    """Create a clean repository and fake quality command under ``base``."""
    base.mkdir(parents=True, exist_ok=True)
    repo = make_repository(base)
    return repo, write_fake_make(base / "fake-make", exit_code=0), base


def run_with_logging_disabled(
    repo: Path,
    base: Path,
    fake_make: Path,
) -> subprocess.CompletedProcess[str]:
    """Run the hook with logging disabled from the start.

    ``base`` supplies the environment's state location, kept in a separate
    ``unused-state`` directory so the counterfactual never sees the enabled
    run's log directory.
    """
    env = hook_environment(base, base / "unused-state", fake_make)
    env["POST_TURN_LOG"] = "0"
    return run_hook_in(repo, env)


@dataclasses.dataclass(frozen=True)
class GeneratedLogState:
    """One generated pre-existing state-directory state for a parity run."""

    content: str
    retention_days: int
    age_days: int
    ignore_rule: str | None


def daily_log_states() -> st.SearchStrategy[GeneratedLogState]:
    """Build a generated state-directory state for the parity property."""
    return st.builds(
        GeneratedLogState,
        content=st.sampled_from(["", hook_record_line(), "notes here\n"]),
        retention_days=st.integers(min_value=0, max_value=20),
        age_days=st.integers(min_value=0, max_value=40),
        ignore_rule=st.sampled_from([None, "state/", "**/log/"]),
    )


def prepare_parity_repository(
    tmp_path: Path, case: GeneratedLogState
) -> tuple[Path, Path, Path, Path]:
    """Create the repository, the pre-existing daily log, and the fake gate.

    Hypothesis reuses the pytest fixture across examples, so every example gets
    its own directory tree and repository. The state directory sits inside the
    worktree, which is the only placement where the log could reach a gate.
    """
    base = pathlib.Path(tempfile.mkdtemp(dir=tmp_path))
    repo = make_repository(base)
    fake_make = write_fake_make(base / "fake-make", exit_code=0)
    if case.ignore_rule is not None:
        (repo / ".gitignore").write_text(f"{case.ignore_rule}\n", encoding="utf-8")
        git_run(repo, "add", ".gitignore")
        git_run(repo, "commit", "-q", "-m", "ignore the state directory")
    state_home = repo / "state"
    log_dir = state_home / "post-turn-quality-stop-hook" / "log"
    log_dir.mkdir(parents=True)
    log_date = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=case.age_days)
    daily = log_dir / f"hook-{log_date.isoformat()}.jsonl"
    daily.write_text(case.content, encoding="utf-8")
    write_xdg_config(
        base, f"log_enabled = true\nlog_retention_days = {case.retention_days}\n"
    )
    return repo, fake_make, state_home, daily


def is_owned_daily_file(case: GeneratedLogState) -> bool:
    """Return whether the generated file is one an enabled logging run owns.

    A file made entirely of hook records is verified as the hook's own whatever
    its date. An empty file proves nothing: an earlier crash that wrote no line
    and a file someone else created at the same path look identical, so it is
    refused and left as it is. Any other content is ordinary repository content.
    """
    return case.content == hook_record_line()


def expects_deletion(case: GeneratedLogState) -> bool:
    """Return whether retention is allowed to remove the generated daily file.

    Only a Git-ignored file can be removed without changing what the gates see,
    and only a file the hook can prove it wrote is its to remove.
    """
    return (
        case.ignore_rule is not None
        and case.age_days > case.retention_days
        and is_owned_daily_file(case)
    )


def state_directory_snapshot(state_home: Path) -> dict[str, str]:
    """Return every file under ``state_home`` and its content."""
    if not state_home.is_dir():
        return {}
    return {
        str(path.relative_to(state_home)): path.read_text(
            encoding="utf-8", errors="surrogatepass"
        )
        for path in sorted(state_home.rglob("*"))
        if path.is_file()
    }


def write_xdg_config(tmp_path: Path, body: str) -> None:
    """Write an XDG configuration file for the hook process."""
    path = tmp_path / "config" / "post-turn-quality-stop-hook" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def fetch_commands(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return the fetch commands recorded by a completed run."""
    commands = typ.cast("list[dict[str, object]]", finish_record(records)["commands"])
    return [command for command in commands if command["kind"] == "fetch"]


def write_slow_make(path: Path, marker: Path) -> Path:
    """Write an executable that announces itself, then outlives the signal."""
    path.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nsleep 20\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def write_blocking_make(path: Path, marker: Path, release: Path) -> Path:
    """Write an executable that announces itself, then waits for the test.

    The fixed sleep in :func:`write_slow_make` only bounds how long the child
    stays alive. A build that blocks reading ``release`` stays running however
    long the test takes, so a signal sent after the marker really does interrupt
    a live command.
    """
    path.write_text(
        "#!/bin/sh\n"
        f"touch {shlex.quote(str(marker))}\n"
        f"cat {shlex.quote(str(release))} >/dev/null\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def release_blocked_make(release: Path) -> None:
    """Release a build child blocked reading ``release``, if one is waiting.

    The writer is opened non-blocking so cleanup cannot hang when the child has
    already exited; ``ENXIO`` simply means nobody is waiting.
    """
    try:
        descriptor = os.open(release, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    os.close(descriptor)


def open_fifo_writer(path: Path, timeout: float = 20.0) -> int:
    """Open ``path`` for writing once a reader is waiting, or fail the test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            time.sleep(0.05)
    pytest.fail("the hook never opened the stalled configuration file")


def wait_for_file(path: Path, timeout: float = 20.0) -> bool:
    """Wait until ``path`` exists, reporting whether it appeared."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False
