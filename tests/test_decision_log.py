"""Exercise the durable per-invocation decision log."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
import time
import typing as typ
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest

from post_turn_quality_stop_hook import decision_log as log_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.decision_log import (
    MAX_REASON_CHARS,
    DecisionLog,
    DecisionLogDeps,
    DecisionLogSettings,
    InvocationContext,
    SignalGuard,
    make_decision_log,
    parse_retention_days,
    prune_old_logs,
    settings_from_env,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_RETENTION = 14
CONFIG_RETENTION = 3
ENV_RETENTION = 2
FALLBACK_RETENTION = 9
# The injected clock starts 0.5 s after the supplied ``started`` timestamp.
EXPECTED_DURATION_MS = 500

CORRELATION: dict[str, object] = {
    "session_id": "session-1",
    "transcript_path": "/logs/transcript.jsonl",
    "hook_event_name": "Stop",
    "stop_hook_active": False,
}

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


def _settings(
    tmp_path: Path, *, enabled: bool = True, retention_days: int = 14
) -> DecisionLogSettings:
    """Return settings pointing at a temporary state home."""
    return DecisionLogSettings(
        enabled=enabled,
        state_home=tmp_path / "state",
        retention_days=retention_days,
    )


def _log_files(state_home: Path) -> list[Path]:
    """Return decision-log files beneath a state home, oldest first."""
    return sorted(log_mod.log_directory(state_home).glob("hook-*.jsonl"))


def _read_records(state_home: Path) -> list[dict[str, object]]:
    """Parse every JSONL record written beneath a state home."""
    return [
        typ.cast("dict[str, object]", json.loads(line))
        for path in _log_files(state_home)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _make_log(settings: DecisionLogSettings) -> DecisionLog:
    """Construct a decision log with standard correlation fields."""
    return DecisionLog(
        settings,
        log_mod.InvocationContext(
            correlation=CORRELATION,
            start_dir=Path("/repo"),
            base_ref="origin/main",
        ),
    )


def _finish_record(records: list[dict[str, object]]) -> dict[str, object]:
    """Return the single finish record from a run."""
    finishes = [record for record in records if record["record_type"] == "finish"]
    assert len(finishes) == 1, "expected exactly one finish record"
    return finishes[0]


# ---------------------------------------------------------------------------
# Settings and environment
# ---------------------------------------------------------------------------


class TestSettings:
    """Resolve settings from configuration and the environment."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, 14),
            ("", 14),
            ("  ", 14),
            ("not-a-number", 14),
            ("-3", 14),
            ("0", 0),
            ("7", 7),
            (" 21 ", 21),
        ],
    )
    def test_parse_retention_days(self, raw: str | None, expected: int) -> None:
        """Only non-negative integers override the default."""
        assert parse_retention_days(raw, 14) == expected

    def test_defaults_enable_logging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Logging is on by default beneath ``XDG_STATE_HOME``."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))

        settings = settings_from_env()

        assert settings.enabled is True
        assert settings.state_home == tmp_path / "xdg"
        assert settings.retention_days == DEFAULT_RETENTION

    def test_state_home_falls_back_to_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``~/.local/state`` is used when ``XDG_STATE_HOME`` is unset."""
        monkeypatch.delenv("XDG_STATE_HOME")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        settings = settings_from_env()

        assert settings.state_home == tmp_path / "home" / ".local" / "state"

    def test_relative_state_home_falls_back_to_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative ``XDG_STATE_HOME`` is ignored, as the XDG spec requires.

        Honouring it would put the log inside the repository that the hook is
        checking, and the uncommitted-changes gate would then block the run on
        the log file itself.
        """
        monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        settings = settings_from_env()

        assert settings.state_home == tmp_path / "home" / ".local" / "state"

    def test_config_values_apply(self, tmp_path: Path) -> None:
        """Configuration can disable logging and set retention."""
        settings = settings_from_env(
            Config(log_enabled=False, log_retention_days=CONFIG_RETENTION)
        )

        assert settings.enabled is False
        assert settings.retention_days == CONFIG_RETENTION

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1", True),
            ("true", True),
            ("yes", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("no", False),
            ("off", False),
        ],
    )
    def test_environment_toggle_overrides_config(
        self,
        raw: str,
        expected: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``POST_TURN_LOG`` overrides the config value in either direction."""
        monkeypatch.setenv("POST_TURN_LOG", raw)

        settings = settings_from_env(Config(log_enabled=not expected))

        assert settings.enabled is expected

    def test_unrecognized_toggle_keeps_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unparseable toggle is ignored rather than guessed."""
        monkeypatch.setenv("POST_TURN_LOG", "maybe")

        settings = settings_from_env(Config(log_enabled=False))

        assert settings.enabled is False

    def test_environment_retention_overrides_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``POST_TURN_LOG_RETENTION_DAYS`` wins over the config value."""
        monkeypatch.setenv("POST_TURN_LOG_RETENTION_DAYS", str(ENV_RETENTION))

        settings = settings_from_env(Config(log_retention_days=FALLBACK_RETENTION))

        assert settings.retention_days == ENV_RETENTION

    def test_invalid_environment_retention_keeps_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An invalid environment retention falls back to the config value."""
        monkeypatch.setenv("POST_TURN_LOG_RETENTION_DAYS", "soon")

        settings = settings_from_env(Config(log_retention_days=FALLBACK_RETENTION))

        assert settings.retention_days == FALLBACK_RETENTION

    def test_disabled_settings_create_no_log(self, tmp_path: Path) -> None:
        """``make_decision_log`` returns ``None`` when logging is disabled."""
        log = make_decision_log(
            _settings(tmp_path, enabled=False),
            InvocationContext(
                correlation=CORRELATION,
                start_dir=Path("/repo"),
                base_ref="origin/main",
            ),
        )

        assert log is None
        assert _log_files(tmp_path / "state") == []


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestRetention:
    """Prune expired daily files."""

    def test_prunes_only_expired_log_files(self, tmp_path: Path) -> None:
        """Old dated files are removed; recent and unrelated files stay."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        today = dt.date(2026, 11, 6)
        expired = log_dir / "hook-2026-10-20.jsonl"
        boundary = log_dir / f"hook-{today.isoformat()}.jsonl"
        recent = log_dir / "hook-2026-11-01.jsonl"
        unrelated = log_dir / "notes.txt"
        malformed = log_dir / "hook-not-a-date.jsonl"
        for path in (expired, boundary, recent, unrelated, malformed):
            path.write_text("", encoding="utf-8")

        removed = prune_old_logs(log_dir, 7, today=today)

        assert removed == 1
        assert not expired.exists()
        assert boundary.exists()
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
        yesterday.write_text("", encoding="utf-8")
        current.write_text("", encoding="utf-8")

        removed = prune_old_logs(log_dir, 0, today=today)

        assert removed == 1
        assert not yesterday.exists()
        assert current.exists()

    def test_negative_retention_is_ignored(self, tmp_path: Path) -> None:
        """A negative retention never deletes anything."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        old = log_dir / "hook-2020-01-01.jsonl"
        old.write_text("", encoding="utf-8")

        assert prune_old_logs(log_dir, -1) == 0
        assert old.exists()

    def test_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        """A missing log directory is reported as zero removals."""
        assert prune_old_logs(tmp_path / "absent", 14) == 0

    def test_retention_beyond_the_date_range_keeps_files(self, tmp_path: Path) -> None:
        """A retention window past the calendar range prunes nothing."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        old = log_dir / "hook-2020-01-01.jsonl"
        old.write_text("", encoding="utf-8")

        assert prune_old_logs(log_dir, 10_000_000) == 0
        assert old.exists()

    def test_construction_with_huge_retention_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        """An extreme retention value never aborts log construction."""
        log = _make_log(_settings(tmp_path, retention_days=10_000_000))

        log.finish()

        assert _read_records(tmp_path / "state")[0]["record_type"] == "start"


# ---------------------------------------------------------------------------
# Record contents
# ---------------------------------------------------------------------------


class TestRecords:
    """Records carry the documented fields and no command output."""

    def test_short_write_warns_without_retrying(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A partial append warns instead of silently truncating a record."""
        log = _make_log(_settings(tmp_path))
        with mock.patch.object(log_mod.os, "write", return_value=1) as write:
            log.finish()

        assert write.call_count == 1, "a short write must not be retried"
        assert "could not write the complete decision log record" in (
            capsys.readouterr().err
        )

    def test_undecodable_command_warns_instead_of_raising(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A command holding undecodable bytes cannot change the decision."""
        log = _make_log(_settings(tmp_path))

        log.command_start("make", "tool-\udcff", ["all"])

        assert "could not write the decision log" in capsys.readouterr().err

    def test_start_record_carries_correlation(self, tmp_path: Path) -> None:
        """The start record joins the run to its Claude Code transcript."""
        log = _make_log(_settings(tmp_path))

        records = _read_records(tmp_path / "state")

        start = records[0]
        assert start["record_type"] == "start"
        assert start["run_id"] == log.run_id
        assert start["session_id"] == "session-1"
        assert start["transcript_path"] == "/logs/transcript.jsonl"
        assert start["hook_event_name"] == "Stop"
        assert start["stop_hook_active"] is False
        assert start["hook_version"]
        assert start["start_dir"] == "/repo"
        assert start["base_ref"] == "origin/main"
        assert start["pid"] == os.getpid()

    def test_correlation_known_after_construction_is_recorded(
        self, tmp_path: Path
    ) -> None:
        """The payload's correlation is durable as soon as it is known."""
        log = DecisionLog(
            _settings(tmp_path),
            log_mod.InvocationContext(
                correlation={},
                start_dir=Path("/repo"),
                base_ref="origin/main",
            ),
        )

        log.set_correlation({"session_id": "session-1"})
        log.finish()

        records = _read_records(tmp_path / "state")
        assert "session_id" not in records[0], (
            "the start record predates the payload read"
        )
        correlation = next(
            record for record in records if record.get("event") == "correlation"
        )
        assert correlation["session_id"] == "session-1"
        finish = _finish_record(records)
        assert finish["session_id"] == "session-1"

    def test_stage_progress_is_written_on_start_and_finish(
        self, tmp_path: Path
    ) -> None:
        """A stage records progress before its work begins."""
        log = _make_log(_settings(tmp_path))

        with log.stage("prepare"):
            progress = _read_records(tmp_path / "state")

        assert progress[-1]["event"] == "stage_start"
        assert progress[-1]["stage"] == "prepare"
        log.finish()

        finish = _finish_record(_read_records(tmp_path / "state"))
        stages = typ.cast("dict[str, object]", finish["stages"])
        assert isinstance(stages["prepare"], int)
        events = [
            record.get("event")
            for record in _read_records(tmp_path / "state")
            if record.get("event") is not None
        ]
        assert events == ["stage_start", "stage_finish"]

    def test_command_records_exclude_output(self, tmp_path: Path) -> None:
        """Commands record exit status and timing, never captured output."""
        log = _make_log(_settings(tmp_path))
        progress = log.command_start("code", "make test", ["test"])

        in_flight = _read_records(tmp_path / "state")
        assert in_flight[-1]["event"] == "command_start"
        log.command_finish(progress, 2)
        log.finish()

        finish = _finish_record(_read_records(tmp_path / "state"))
        commands = typ.cast("list[dict[str, object]]", finish["commands"])
        command = commands[0]
        assert command == {
            "kind": "code",
            "cmd": "make test",
            "targets": ["test"],
            "exit_code": 2,
            "duration_ms": command["duration_ms"],
        }
        serialized = json.dumps(finish)
        assert "stdout" not in serialized
        assert "stderr" not in serialized

    def test_finish_lists_every_gate_in_order(self, tmp_path: Path) -> None:
        """Unreached gates are recorded explicitly, not omitted."""
        log = _make_log(_settings(tmp_path))
        log.record_gate("quality_checks", "pass")
        log.record_gate("uncommitted_changes", "skip", "gate disabled")
        log.finish()

        finish = _finish_record(_read_records(tmp_path / "state"))

        gates = typ.cast("list[dict[str, object]]", finish["gates"])
        assert [gate["gate"] for gate in gates] == list(log_mod.GATE_ORDER)
        assert gates[0]["outcome"] == "pass"
        assert gates[1] == {
            "gate": "uncommitted_changes",
            "outcome": "skip",
            "reason": "gate disabled",
        }
        assert gates[2]["outcome"] == "not_reached"
        assert gates[3]["outcome"] == "not_reached"

    def test_mark_block_sets_decision_and_bounded_reason(self, tmp_path: Path) -> None:
        """A block decision records a single-line, bounded reason."""
        log = _make_log(_settings(tmp_path))
        reason = "line one\nline two " + "x" * MAX_REASON_CHARS

        log.mark_block(reason)
        log.finish()

        finish = _finish_record(_read_records(tmp_path / "state"))
        assert finish["decision"] == "block"
        bounded = typ.cast("str", finish["block_reason"])
        assert "\n" not in bounded
        assert len(bounded) == MAX_REASON_CHARS

    def test_finish_records_error_and_exit_code(self, tmp_path: Path) -> None:
        """Unexpected errors are summarized without a traceback."""
        log = _make_log(_settings(tmp_path))

        log.finish(decision="error", exit_code=1, error="ValueError: boom")

        finish = _finish_record(_read_records(tmp_path / "state"))
        assert finish["decision"] == "error"
        assert finish["exit_code"] == 1
        assert finish["error"] == "ValueError: boom"

    def test_finish_is_idempotent(self, tmp_path: Path) -> None:
        """A second finish call does not append another record."""
        log = _make_log(_settings(tmp_path))

        log.finish()
        log.finish()

        records = _read_records(tmp_path / "state")
        assert sum(record["record_type"] == "finish" for record in records) == 1

    def test_each_record_is_a_single_json_line(self, tmp_path: Path) -> None:
        """Records are appended one line at a time for concurrent readers."""
        log = _make_log(_settings(tmp_path))
        log.set_repository(branch="main")
        log.set_quality(build_driver="make")
        log.finish()

        path = _log_files(tmp_path / "state")[0]
        lines = path.read_text(encoding="utf-8").splitlines()

        assert len(lines) == len(_read_records(tmp_path / "state"))
        for line in lines:
            assert isinstance(json.loads(line), dict)

    def test_write_failure_warns_without_raising(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An unwritable location never changes the hook decision."""
        blocker = tmp_path / "state"
        blocker.write_text("not a directory", encoding="utf-8")
        settings = DecisionLogSettings(state_home=blocker)

        log = _make_log(settings)
        log.finish()

        assert "could not write the decision log" in capsys.readouterr().err

    def test_concurrent_runs_do_not_interleave(self, tmp_path: Path) -> None:
        """Many processes' worth of records remain individually parseable."""
        settings = _settings(tmp_path)

        def run_one(_index: int) -> str:
            log = _make_log(settings)
            log.record_gate("quality_checks", "pass")
            log.finish()
            return log.run_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            run_ids = list(pool.map(run_one, range(24)))

        records = _read_records(tmp_path / "state")
        assert len(records) == len(run_ids) * 2
        assert len(set(run_ids)) == len(run_ids)
        starts = {
            record["run_id"] for record in records if record["record_type"] == "start"
        }
        finishes = {
            record["run_id"] for record in records if record["record_type"] == "finish"
        }
        assert starts == finishes == set(run_ids)

    def test_retention_runs_on_each_invocation(self, tmp_path: Path) -> None:
        """Constructing a log prunes files older than the retention window."""
        state_home = tmp_path / "state"
        log_dir = log_mod.log_directory(state_home)
        log_dir.mkdir(parents=True)
        stale = log_dir / "hook-2000-01-01.jsonl"
        stale.write_text("", encoding="utf-8")

        _make_log(_settings(tmp_path))

        assert not stale.exists()


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


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
    subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: fixed Git argv, no shell.
        ["/usr/bin/git", *args],
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


# ---------------------------------------------------------------------------
# Retention diagnostics and injected boundaries
# ---------------------------------------------------------------------------


class TestRetentionDiagnostics:
    """Retention failures are reported with fixed, path-free messages."""

    def test_scan_failure_is_reported(self, tmp_path: Path) -> None:
        """A directory that cannot be scanned reports one fixed message."""
        messages: list[str] = []
        with mock.patch.object(log_mod.Path, "iterdir", side_effect=OSError("boom")):
            removed = prune_old_logs(tmp_path, 14, on_error=messages.append)

        assert removed == 0
        assert messages == ["could not scan the decision-log directory"]

    def test_delete_failure_is_reported(self, tmp_path: Path) -> None:
        """A file that cannot be removed reports one fixed message."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        (log_dir / "hook-2020-01-01.jsonl").write_text("", encoding="utf-8")
        messages: list[str] = []

        with mock.patch.object(log_mod.Path, "unlink", side_effect=OSError("boom")):
            removed = prune_old_logs(
                log_dir, 1, today=dt.date(2026, 11, 6), on_error=messages.append
            )

        assert removed == 0
        assert messages == ["could not remove an expired decision-log file"]

    def test_jsonl_sink_reports_retention_failures_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The default JSONL sink routes retention failures to stderr."""
        sink = log_mod.JsonlRecordSink(tmp_path / "log", 14)
        with mock.patch.object(log_mod.Path, "iterdir", side_effect=OSError("boom")):
            sink.prune(dt.date(2026, 11, 6))

        captured = capsys.readouterr()
        assert "could not scan the decision-log directory" in captured.err
        assert captured.out == "", "stdout stays reserved for the hook JSON"


class _FixedClock:
    """Clock returning fixed wall and monotonic values."""

    def __init__(self, wall: dt.datetime, monotonic: float) -> None:
        self._wall = wall
        self._monotonic = monotonic

    def wall(self) -> dt.datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._monotonic


class _RecordingSink:
    """Sink that keeps records in memory."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.pruned: list[dt.date] = []

    def write(self, record: typ.Mapping[str, object]) -> None:
        self.records.append(dict(record))

    def prune(self, today: dt.date) -> None:
        self.pruned.append(today)


class _RaisingSink:
    """Sink that fails every write, as an injected adapter might."""

    def write(self, record: typ.Mapping[str, object]) -> None:
        raise RuntimeError

    def prune(self, today: dt.date) -> None:
        return None


class TestInjectedDependencies:
    """Record collection depends on narrow, substitutable interfaces."""

    def test_injected_clock_and_run_id_shape_the_records(self, tmp_path: Path) -> None:
        """The collector uses the supplied clock and identifier source."""
        wall = dt.datetime(2026, 11, 6, 12, 0, tzinfo=dt.UTC)
        log = DecisionLog(
            _settings(tmp_path),
            InvocationContext(
                correlation={"session_id": "session-1"},
                start_dir=Path("/work"),
                base_ref="origin/main",
            ),
            deps=DecisionLogDeps(
                clock=_FixedClock(wall, 100.0),
                run_id_factory=lambda: "run-fixed",
            ),
            started=99.5,
        )

        log.finish()

        records = _read_records(tmp_path / "state")
        assert records[0]["run_id"] == "run-fixed"
        assert records[0]["timestamp"] == "2026-11-06T12:00:00+00:00"
        assert records[-1]["duration_ms"] == EXPECTED_DURATION_MS

    def test_injected_sink_receives_every_record(self, tmp_path: Path) -> None:
        """Persistence is substitutable without a filesystem."""
        sink = _RecordingSink()
        log = DecisionLog(
            _settings(tmp_path),
            InvocationContext(
                correlation={}, start_dir=Path("/work"), base_ref="origin/main"
            ),
            deps=DecisionLogDeps(
                sink=sink,
                clock=_FixedClock(dt.datetime(2026, 11, 6, tzinfo=dt.UTC), 10.0),
            ),
        )

        with log.stage("prepare"):
            pass
        log.finish()

        assert [record["record_type"] for record in sink.records] == [
            "start",
            "progress",
            "progress",
            "finish",
        ]
        assert sink.pruned == [dt.date(2026, 11, 6)]

    def test_sink_failure_does_not_escape(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A raising sink warns without replacing the decision."""
        log = DecisionLog(
            _settings(tmp_path),
            InvocationContext(
                correlation={}, start_dir=Path("/work"), base_ref="origin/main"
            ),
            deps=DecisionLogDeps(sink=_RaisingSink()),
        )

        with log.stage("prepare"):
            pass
        log.finish(decision="allow", exit_code=0)

        assert "could not record a decision-log entry" in capsys.readouterr().err
