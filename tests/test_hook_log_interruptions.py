"""End-to-end tests for interruption records and signal handling.

The log must be durable before protected work begins, so an interruption leaves
evidence rather than a silent gap. Catchable signals write a best-effort
``interrupted`` finish; SIGKILL cannot, and neither can an unwritable log.
"""

from __future__ import annotations

import os
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import typing as typ

from hook_support import (
    finish_record,
    hook_command,
    hook_environment,
    open_fifo_writer,
    read_records,
    send_hook_input,
    start_hook,
    stop,
    wait_for_file,
    wait_for_record,
    write_slow_make,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_start_record_precedes_a_stalled_input_read(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """The log exists before standard input is read, so a stall is visible."""
    state_home = tmp_path / "state"
    process = start_hook(repo, tmp_path, state_home, fake_make)
    try:
        assert wait_for_record(state_home, record_type="start"), (
            "the start record must be written before the hook reads stdin"
        )
        send_hook_input(process, repo)
        stdout, stderr = stop(process)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hung hook
            process.kill()
            process.communicate()

    assert process.returncode == 0, stderr
    assert stdout == "", "a successful hook writes nothing to stdout"
    assert finish_record(read_records(state_home))["decision"] == "allow"


def test_sigterm_while_the_configuration_read_is_stalled_records_interruption(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """The signal guard covers the configuration read, not only the pipeline."""
    state_home = tmp_path / "state"
    config_fifo = tmp_path / "stalled-config.toml"
    os.mkfifo(config_fifo)
    process = subprocess.Popen(  # ruff:ignore[subprocess-without-shell-equals-true]  # fixed argv, no shell.
        hook_command("--config", str(config_fifo)),
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=hook_environment(tmp_path, state_home, fake_make),
    )
    writer: int | None = None
    try:
        send_hook_input(process, repo)
        # A non-blocking writer open succeeds only once the hook has opened the
        # FIFO for reading, so this pins the signal to the stalled config read.
        writer = open_fifo_writer(config_fifo)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = stop(process)
    finally:
        if writer is not None:
            os.close(writer)
        if process.poll() is None:  # pragma: no cover - only on a hung hook
            process.kill()
            process.communicate()

    assert process.returncode == -signal.SIGTERM, stderr
    assert stdout == "", "an interrupted hook must not print a decision"
    finish = finish_record(read_records(state_home))
    assert finish["decision"] == "interrupted"
    assert finish["signal"] == "SIGTERM"
    assert finish["exit_code"] == 128 + signal.SIGTERM


def test_sigterm_during_a_command_records_interruption(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """SIGTERM while a build command runs leaves an interrupted finish record."""
    slow_make = write_slow_make(fake_make, tmp_path / "make-started")
    state_home = tmp_path / "state"
    process = start_hook(repo, tmp_path, state_home, slow_make)
    try:
        send_hook_input(process, repo)
        assert wait_for_record(
            state_home, record_type="progress", event="command_start"
        ), "the build command must start before the signal is sent"
        assert wait_for_file(tmp_path / "make-started"), (
            "the build command must be running before the signal is sent"
        )
        process.send_signal(signal.SIGTERM)
        stdout, stderr = stop(process)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hung hook
            process.kill()
            process.communicate()

    assert process.returncode == -signal.SIGTERM, stderr
    assert stdout == "", "an interrupted hook must not print a decision"
    finish = finish_record(read_records(state_home))
    assert finish["decision"] == "interrupted"
    assert finish["signal"] == "SIGTERM"
    assert finish["exit_code"] == 128 + signal.SIGTERM


def test_sigkill_during_a_command_leaves_evidence_without_a_finish(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """SIGKILL cannot write a finish record, so the start and command remain."""
    slow_make = write_slow_make(fake_make, tmp_path / "make-started")
    state_home = tmp_path / "state"
    process = start_hook(repo, tmp_path, state_home, slow_make)
    try:
        send_hook_input(process, repo)
        assert wait_for_record(
            state_home, record_type="progress", event="command_start"
        ), "the build command must start before the signal is sent"
        assert wait_for_file(tmp_path / "make-started"), (
            "the build command must be running before the signal is sent"
        )
        process.kill()
        stdout, _stderr = stop(process)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hung hook
            process.kill()
            process.communicate()

    assert process.returncode == -signal.SIGKILL
    assert stdout == "", "a killed hook must not print a decision"
    records = read_records(state_home)
    assert any(record["record_type"] == "start" for record in records)
    assert any(record.get("event") == "command_start" for record in records), (
        "progress must be durable before the protected command finishes"
    )
    assert not any(record["record_type"] == "finish" for record in records), (
        "SIGKILL cannot be caught, so no finish record may appear"
    )


def test_unwritable_log_leaves_a_signal_death_unchanged(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A blocking log location does not change how a signalled hook exits."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")

    def signalled(name: str, state_home: Path) -> tuple[int, str]:
        """Run the hook, signal it mid-build, and return its exit and stdout."""
        marker = tmp_path / f"make-started-{name}"
        slow_make = write_slow_make(fake_make, marker)
        process = start_hook(repo, tmp_path, state_home, slow_make)
        send_hook_input(process, repo)
        # The unwritable run has no log to poll, so wait for the build child
        # itself. Both runs must be signalled at the same point.
        started = wait_for_file(marker)
        process.send_signal(signal.SIGTERM)
        stdout, _stderr = stop(process)
        assert started, "the build command must start before the signal is sent"
        return process.returncode, stdout

    writable_code, writable_stdout = signalled("writable", tmp_path / "state")
    unwritable_code, unwritable_stdout = signalled("unwritable", blocker / "state")

    assert unwritable_code == writable_code == -signal.SIGTERM
    assert unwritable_stdout == writable_stdout == ""
