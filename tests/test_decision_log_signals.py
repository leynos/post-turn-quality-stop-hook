"""Catchable signals leave an interrupted record; SIGKILL cannot."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
import time
import typing as typ
from unittest import mock

import pytest
from decision_log_support import (
    _finish_record,
    _make_log,
    _read_records,
    _settings,
)

from post_turn_quality_stop_hook import decision_log as log_mod
from post_turn_quality_stop_hook.decision_log import SignalGuard

if typ.TYPE_CHECKING:
    from pathlib import Path


_READY_SCRIPT = """
import sys
import time
from pathlib import Path

from post_turn_quality_stop_hook.decision_log import (
    DecisionLog,
    DecisionLogSettings,
    InvocationContext,
    SignalGuard,
)

settings = DecisionLogSettings(state_home=Path(sys.argv[1]))
log = DecisionLog(
    settings,
    InvocationContext(
        correlation={"session_id": "sess-signal"},
        start_dir=Path.cwd(),
        base_ref="origin/main",
    ),
)
with SignalGuard(log.record_signal):
    with log.stage("quality_checks"):
        print("ready", flush=True)
        time.sleep(30)
"""


_READY_SCRIPT = """
import sys
import time
from pathlib import Path

from post_turn_quality_stop_hook.decision_log import (
    DecisionLog,
    DecisionLogSettings,
    InvocationContext,
    SignalGuard,
)

settings = DecisionLogSettings(state_home=Path(sys.argv[1]))
log = DecisionLog(
    settings,
    InvocationContext(
        correlation={"session_id": "sess-signal"},
        start_dir=Path.cwd(),
        base_ref="origin/main",
    ),
)
with SignalGuard(log.record_signal):
    with log.stage("quality_checks"):
        print("ready", flush=True)
        time.sleep(30)
"""


class TestSignals:
    """Catchable signals leave an interrupted record; SIGKILL cannot."""

    def test_sigterm_writes_interrupted_finish_and_reraises(
        self, tmp_path: Path
    ) -> None:
        """The handler records the signal, restores default, then re-raises."""
        log = _make_log(_settings(tmp_path))
        guard = SignalGuard(log.record_signal)

        with (
            mock.patch.object(log_mod.signal, "signal") as install,
            mock.patch.object(log_mod.os, "kill") as kill,
        ):
            guard._handle(signal.SIGTERM, None)

        finish = _finish_record(_read_records(tmp_path / "state"))
        assert finish["decision"] == "interrupted"
        assert finish["signal"] == "SIGTERM"
        assert finish["exit_code"] == 128 + signal.SIGTERM
        install.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)

    def test_sigint_restores_the_previous_handler(self, tmp_path: Path) -> None:
        """SIGINT keeps its unwinding behaviour so a build child is reaped."""
        log = _make_log(_settings(tmp_path))
        previous = signal.default_int_handler
        with (
            mock.patch.object(log_mod.signal, "getsignal", return_value=previous),
            mock.patch.object(log_mod.signal, "signal") as install,
            mock.patch.object(log_mod.os, "kill") as kill,
            SignalGuard(log.record_signal) as guard,
        ):
            guard._handle(signal.SIGINT, None)

        install.assert_any_call(signal.SIGINT, previous)
        kill.assert_called_once_with(os.getpid(), signal.SIGINT)

    def test_native_handler_is_left_alone(self, tmp_path: Path) -> None:
        """A handler Python cannot restore is never replaced."""
        log = _make_log(_settings(tmp_path))
        with (
            mock.patch.object(log_mod.signal, "getsignal", return_value=None),
            mock.patch.object(log_mod.signal, "signal") as install,
            SignalGuard(log.record_signal),
        ):
            pass

        install.assert_not_called()

    def test_ignored_signal_disposition_is_preserved(self, tmp_path: Path) -> None:
        """A signal the parent ignored stays ignored inside the guard."""
        previous = signal.getsignal(signal.SIGHUP)
        try:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            log = _make_log(_settings(tmp_path))

            with SignalGuard(log.record_signal):
                assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN

            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        finally:
            signal.signal(signal.SIGHUP, previous)

    def test_sigkill_leaves_start_and_stage_without_finish(
        self, tmp_path: Path
    ) -> None:
        """SIGKILL mid-stage leaves evidence but no finish record."""
        state_home = tmp_path / "state"
        process = _start_paused_hook(state_home)
        try:
            _await_ready(process)
            process.kill()
            process.wait(timeout=10)
        finally:
            _terminate(process)

        records = _read_records(state_home)
        assert records[0]["record_type"] == "start"
        assert any(record.get("event") == "stage_start" for record in records)
        assert all(record["record_type"] != "finish" for record in records)

    def test_sigterm_mid_stage_records_interrupted(self, tmp_path: Path) -> None:
        """SIGTERM mid-stage leaves an interrupted finish with the signal name."""
        state_home = tmp_path / "state"
        process = _start_paused_hook(state_home)
        try:
            _await_ready(process)
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)
        finally:
            _terminate(process)

        assert process.returncode == -signal.SIGTERM
        finish = _finish_record(_read_records(state_home))
        assert finish["decision"] == "interrupted"
        assert finish["signal"] == "SIGTERM"

    def test_sigkill_during_a_command_leaves_no_finish(self, tmp_path: Path) -> None:
        """SIGKILL inside a real gate leaves the in-flight command visible."""
        process, state_home, ready = _start_in_flight_hook(tmp_path)
        try:
            _await_file(ready)
            process.kill()
            process.wait(timeout=10)
        finally:
            _terminate(process)

        records = _read_records(state_home)
        assert records[0]["record_type"] == "start"
        assert any(
            record.get("event") == "command_start"
            and typ.cast("dict[str, object]", record["command"])["kind"] == "code"
            for record in records
        ), "the in-flight quality command must be recorded"
        assert all(record["record_type"] != "finish" for record in records)

    def test_sigterm_during_a_command_records_interrupted(self, tmp_path: Path) -> None:
        """SIGTERM inside a real gate records the signal and the open command."""
        process, state_home, ready = _start_in_flight_hook(tmp_path)
        try:
            _await_file(ready)
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)
        finally:
            _terminate(process)

        assert process.returncode == -signal.SIGTERM
        records = _read_records(state_home)
        finish = _finish_record(records)
        assert finish["decision"] == "interrupted"
        assert finish["signal"] == "SIGTERM"
        starts = [
            record for record in records if record.get("event") == "command_start"
        ]
        finishes = [
            record for record in records if record.get("event") == "command_finish"
        ]
        assert starts, "the in-flight quality command must be recorded"
        assert len(finishes) < len(starts), (
            "the interrupted command must not have a finish record"
        )


def _git(repo: Path, *args: str) -> None:
    """Run a Git command in a test repository with isolated configuration."""
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    })
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is required for repository fixtures")
    subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: fixed Git argv, no shell.
        [git, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _prepare_in_flight_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a repository whose quality command blocks until signalled."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "initial")
    (repo / "Makefile").write_text(
        "check-fmt:\n\t@true\nlint:\n\t@true\ntypecheck:\n\t@true\n",
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    (repo / "src" / "foo.py").write_text("x = 1\n", encoding="utf-8")

    ready = tmp_path / "ready"
    fake_make = tmp_path / "fake-make"
    fake_make.write_text(
        f"#!/bin/sh\ntouch '{ready}'\nsleep 10\n",
        encoding="utf-8",
    )
    fake_make.chmod(0o755)
    return repo, ready, fake_make


def _start_in_flight_hook(
    tmp_path: Path,
) -> tuple[subprocess.Popen[str], Path, Path]:
    """Start the real hook with a quality command that blocks until signalled."""
    repo, ready, fake_make = _prepare_in_flight_repo(tmp_path)
    state_home = tmp_path / "state"
    env = os.environ.copy()
    env.update({
        "XDG_STATE_HOME": str(state_home),
        "POST_TURN_BASE_REF": "HEAD",
        "POST_TURN_BUILD_DRIVER": "make",
        "POST_TURN_MAKE_BIN": str(fake_make),
    })
    env.pop("POST_TURN_LOG", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "post_turn_quality_stop_hook.hook"],
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps({"cwd": str(repo), "session_id": "sess-signal"}))
    process.stdin.close()
    return process, state_home, ready


def _await_file(path: Path, timeout: float = 30.0) -> None:
    """Block until a file appears, or fail with a bounded wait."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    message = f"hook did not reach the in-flight command: {path}"
    raise AssertionError(message)


def _start_paused_hook(state_home: Path) -> subprocess.Popen[str]:
    """Start a hook process paused mid-stage."""
    return subprocess.Popen(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: fixed interpreter and in-memory script, no shell.
        [sys.executable, "-c", _READY_SCRIPT, str(state_home)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _await_ready(process: subprocess.Popen[str]) -> None:
    """Block until the child has written its stage-start record."""
    assert process.stdout is not None
    line = process.stdout.readline()
    assert line.strip() == "ready", f"hook did not start: {line!r}"


def _terminate(process: subprocess.Popen[str]) -> None:
    """Ensure a child process is gone, even after an assertion failure."""
    if process.poll() is None:
        process.kill()
        process.wait(timeout=10)
