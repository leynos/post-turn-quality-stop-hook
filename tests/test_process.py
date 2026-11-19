"""Exercise command execution and process cleanup under a timeout."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
from proc_stat import process_state

from post_turn_quality_stop_hook import process as process_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess with the given return code and output."""
    return subprocess.CompletedProcess(
        args=["unit-test"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _is_alive(pid: int) -> bool:
    """Return whether a process is running, without signalling it.

    ``kill(pid, 0)`` also succeeds for a process that has exited but not yet
    been reaped, so it cannot distinguish a survivor from a zombie. A killed
    descendant is reparented and may stay unreaped for a while, which would
    make the wait loop below time out even though the group kill worked. On
    Linux the process state in ``/proc`` separates the two cases.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.parent.is_dir():
        # No /proc: fall back to the existence check above.
        return True
    try:
        stat_line = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    # ``Z`` is an exited, unreaped child, which counts as gone here. An
    # unparsable line is treated as alive so the caller keeps waiting rather
    # than declaring a survivor gone.
    return process_state(stat_line) != "Z"


REPO = Path("/fake/repo")
TIMEOUT_RC = 124

requires_process_groups = pytest.mark.skipif(
    not process_mod._SUPPORTS_PROCESS_GROUPS or not hasattr(signal, "SIGKILL"),
    reason="requires POSIX process-group signalling",
)

requires_proc_stat = pytest.mark.skipif(
    not Path("/proc/self/stat").is_file(), reason="requires Linux /proc"
)

# ---------------------------------------------------------------------------
# run_build_targets
# ---------------------------------------------------------------------------


class TestRunDecoding:
    """Command output is decoded leniently so a bad byte cannot abort the hook."""

    def test_invalid_utf8_output_is_replaced_not_raised(self, tmp_path: Path) -> None:
        """A failing command may emit bytes that are not valid UTF-8."""
        script = (
            "import sys;"
            "sys.stdout.buffer.write(b'out \\xff');"
            "sys.stderr.buffer.write(b'err \\xfe');"
            "sys.exit(3)"
        )

        result = process_mod.run([sys.executable, "-c", script], tmp_path)

        assert result.returncode != 0, (
            f"expected the command to fail but got rc {result.returncode!r}"
        )
        assert "\ufffd" in result.stdout, "stdout must be decoded with replacement"
        assert "\ufffd" in result.stderr, "stderr must be decoded with replacement"


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


class TestRun:
    """Tests for run() subprocess construction keywords.

    Kills the ``run()`` keyword-construction survivors tracked in #30.
    """

    def test_returns_text_output(self, tmp_path: Path) -> None:
        """text=True yields str (not bytes) stdout."""
        result = process_mod.run(["printf", "hello"], tmp_path)
        assert result.returncode == 0
        assert isinstance(result.stdout, str), (
            f"expected str stdout but got {type(result.stdout)!r}"
        )
        assert result.stdout == "hello"

    def test_nonzero_exit_does_not_raise(self, tmp_path: Path) -> None:
        """check=False lets a non-zero exit return rather than raise."""
        nonzero = 3
        result = process_mod.run(["sh", "-c", f"exit {nonzero}"], tmp_path)
        assert result.returncode == nonzero, (
            f"expected returncode {nonzero} without raising "
            f"but got {result.returncode!r}"
        )

    def test_enriched_path_reaches_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env=_subprocess_env() propagates the enriched PATH to the child."""
        tool_dir = tmp_path / ".local" / "bin"
        tool_dir.mkdir(parents=True)
        monkeypatch.setattr(
            process_mod.Path, "home", classmethod(lambda _cls: tmp_path)
        )
        monkeypatch.setenv("PATH", "/usr/bin")

        result = process_mod.run(["sh", "-c", "printf '%s' \"$PATH\""], tmp_path)

        assert str(tool_dir) in result.stdout.split(os.pathsep), (
            f"expected enriched PATH in child env but got {result.stdout!r}"
        )

    def test_timeout_returns_failed_process(self, tmp_path: Path) -> None:
        """A timed-out command is reported as rc=124 rather than raising."""
        result = process_mod.run(["sleep", "5"], tmp_path, timeout=0.01)
        assert result.returncode == TIMEOUT_RC, (
            f"expected returncode {TIMEOUT_RC} for timeout "
            f"but got {result.returncode!r}"
        )
        assert result.stdout == ""
        assert "timed out" in result.stderr, (
            f"expected timeout message in stderr but got {result.stderr!r}"
        )

    def test_close_streams_leaves_a_live_child_alone(self) -> None:
        """A live child's reader thread may still hold the stream lock."""
        sleep = shutil.which("sleep") or "sleep"
        proc = subprocess.Popen(  # ruff:ignore[subprocess-without-shell-equals-true]
            [sleep, "5"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            process_mod._close_streams(proc)
            assert proc.stdout is not None
            assert not proc.stdout.closed, "a live child's stdout must stay open"
            assert proc.stderr is not None
            assert not proc.stderr.closed, "a live child's stderr must stay open"
        finally:
            proc.kill()
            proc.communicate()

    def test_close_streams_closes_an_exited_child(self) -> None:
        """An exited child's pipes are closed to release descriptors."""
        shell = shutil.which("sh") or "sh"
        proc = subprocess.Popen(  # ruff:ignore[subprocess-without-shell-equals-true]
            [shell, "-c", "exit 0"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.wait()
        process_mod._close_streams(proc)
        assert proc.stdout is not None
        assert proc.stdout.closed, "an exited child's stdout must be closed"
        assert proc.stderr is not None
        assert proc.stderr.closed, "an exited child's stderr must be closed"

    @requires_process_groups
    def test_timeout_terminates_group_before_killing(self, tmp_path: Path) -> None:
        """A timed-out fetch is asked to stop before its group is killed."""
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
            ("", ""),
        ]
        with (
            mock.patch.object(process_mod.subprocess, "Popen", return_value=proc),
            mock.patch.object(process_mod.os, "getpgid", return_value=9876) as getpgid,
            mock.patch.object(process_mod.os, "killpg") as killpg,
            mock.patch.object(
                process_mod, "_process_group_alive", side_effect=[True, False]
            ),
        ):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        getpgid.assert_called_once_with(4321)
        assert killpg.call_args_list == [
            mock.call(9876, process_mod.signal.SIGTERM),
            mock.call(9876, process_mod.signal.SIGKILL),
        ]
        assert result.returncode == TIMEOUT_RC
        assert "timed out" in result.stderr

    @requires_process_groups
    def test_timeout_does_not_kill_a_process_that_stops(self, tmp_path: Path) -> None:
        """A command that exits after SIGTERM is not sent SIGKILL."""
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            ("", ""),
        ]
        with (
            mock.patch.object(process_mod.subprocess, "Popen", return_value=proc),
            mock.patch.object(process_mod.os, "getpgid", return_value=9876),
            mock.patch.object(process_mod.os, "killpg") as killpg,
            # Every group member is gone, so no escalation is needed.
            mock.patch.object(process_mod, "_process_group_alive", return_value=False),
        ):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        killpg.assert_called_once_with(9876, process_mod.signal.SIGTERM)
        assert result.returncode == TIMEOUT_RC

    @requires_process_groups
    @requires_proc_stat
    def test_timeout_kills_a_descendant_that_ignores_sigterm(
        self, tmp_path: Path
    ) -> None:
        """A surviving descendant is killed after the leader exits."""
        shell = shutil.which("sh") or "sh"
        pid_file = tmp_path / "descendant.pid"
        # The leader exits on SIGTERM, but the background subshell ignores it.
        # Ignored dispositions survive exec, so the sleep loop inherits the
        # ignore. The subshell redirects its output, so only the leader holds
        # the captured pipes once it is gone.
        # The loop is bounded so a cleanup regression cannot leave a stray
        # SIGTERM-ignoring process behind after the test fails. Sixty seconds
        # is well beyond the five-second assertion window below.
        script = (
            "trap 'exit 0' TERM; "
            "( trap '' TERM; i=0; "
            "while [ $i -lt 120 ]; do sleep 0.5; i=$((i + 1)); done ) "
            ">/dev/null 2>&1 & "
            f"echo $! > {shlex.quote(str(pid_file))}; "
            "wait"
        )

        result = process_mod.run([shell, "-c", script], tmp_path, timeout=0.5)

        assert result.returncode == TIMEOUT_RC
        descendant = int(pid_file.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 5.0
        while _is_alive(descendant) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _is_alive(descendant), (
            "the timed-out command's descendant must not survive the group kill"
        )

    @requires_process_groups
    def test_timeout_falls_back_when_group_lookup_fails(self, tmp_path: Path) -> None:
        """A vanished process group falls back to signalling the direct child."""
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
            ("", ""),
        ]
        with (
            mock.patch.object(process_mod.subprocess, "Popen", return_value=proc),
            mock.patch.object(
                process_mod.os, "getpgid", side_effect=ProcessLookupError
            ) as getpgid,
            mock.patch.object(process_mod.os, "killpg") as killpg,
        ):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        getpgid.assert_called_once_with(4321)
        killpg.assert_not_called()
        assert proc.send_signal.call_args_list == [
            mock.call(process_mod.signal.SIGTERM),
            mock.call(process_mod.signal.SIGKILL),
        ]
        assert result.returncode == TIMEOUT_RC

    @requires_process_groups
    def test_timeout_without_process_group_support_signals_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows has no process groups, so the direct child is signalled."""
        monkeypatch.setattr(process_mod, "_SUPPORTS_PROCESS_GROUPS", False)
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
            ("", ""),
        ]
        with (
            mock.patch.object(process_mod.subprocess, "Popen", return_value=proc),
            mock.patch.object(process_mod.os, "getpgid") as getpgid,
            mock.patch.object(process_mod.os, "killpg") as killpg,
        ):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        getpgid.assert_not_called()
        killpg.assert_not_called()
        assert proc.send_signal.call_args_list == [
            mock.call(process_mod.signal.SIGTERM),
            mock.call(process_mod.signal.SIGKILL),
        ]
        assert result.returncode == TIMEOUT_RC

    def test_timeout_without_sigkill_kills_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A platform without SIGKILL terminates the direct child instead."""
        monkeypatch.setattr(process_mod, "_SUPPORTS_PROCESS_GROUPS", False)
        fake_signal = mock.Mock(spec=[])
        fake_signal.SIGTERM = signal.SIGTERM
        monkeypatch.setattr(process_mod, "signal", fake_signal)
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
            ("", ""),
        ]
        with mock.patch.object(process_mod.subprocess, "Popen", return_value=proc):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.kill.assert_called_once_with()
        assert result.returncode == TIMEOUT_RC

    @requires_process_groups
    def test_timeout_leaves_a_surviving_child_s_streams_open(
        self, tmp_path: Path
    ) -> None:
        """A child that survives SIGKILL keeps its pipes until teardown.

        Closing the pipes of a live child would block on a reader thread that
        cannot finish, so cleanup must skip them and let process teardown
        reclaim the descriptors.
        """
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        # The child is still running, so ``poll()`` must report no exit.
        proc.poll.return_value = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
        ]
        stdout = mock.MagicMock()
        stderr = mock.MagicMock()
        proc.stdout = stdout
        proc.stderr = stderr
        with (
            mock.patch.object(process_mod.subprocess, "Popen", return_value=proc),
            mock.patch.object(process_mod.os, "getpgid", return_value=9876),
            mock.patch.object(process_mod.os, "killpg"),
            # The bounded group wait expires without the group emptying.
            mock.patch.object(process_mod, "_wait_for_group_exit", return_value=False),
        ):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        stdout.close.assert_not_called()
        stderr.close.assert_not_called()
        assert result.returncode == TIMEOUT_RC

    @requires_process_groups
    def test_timeout_falls_back_when_group_vanishes(self, tmp_path: Path) -> None:
        """A group that disappears before signalling uses the direct child."""
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.returncode = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            subprocess.TimeoutExpired(["git"], 0.01),
            ("", ""),
        ]
        with (
            mock.patch.object(process_mod.subprocess, "Popen", return_value=proc),
            mock.patch.object(process_mod.os, "getpgid", return_value=9876),
            mock.patch.object(
                process_mod.os, "killpg", side_effect=ProcessLookupError
            ) as killpg,
        ):
            result = process_mod.run(["git", "fetch"], tmp_path, timeout=0.01)

        # Signal 0 is the group-existence probe; only real signals are asserted.
        signalled = [c for c in killpg.call_args_list if c.args[1] != 0]
        assert signalled == [
            mock.call(9876, process_mod.signal.SIGTERM),
            mock.call(9876, process_mod.signal.SIGKILL),
        ]
        assert proc.send_signal.call_args_list == [
            mock.call(process_mod.signal.SIGTERM),
            mock.call(process_mod.signal.SIGKILL),
        ]
        assert result.returncode == TIMEOUT_RC

    def test_not_a_directory_fallback(self, tmp_path: Path) -> None:
        """A file used as cwd yields the rc=1 fallback, not an exception."""
        file_path = tmp_path / "afile"
        file_path.write_text("", encoding="utf-8")
        result = process_mod.run(["git", "status"], file_path)
        assert result.returncode == 1, (
            f"expected returncode 1 for file cwd but got {result.returncode!r}"
        )
        assert result.args == ["git", "status"]
        assert result.stdout == ""
        assert str(file_path) in result.stderr, (
            f"expected the offending path in stderr but got {result.stderr!r}"
        )


class TestIsAlive:
    """Tests for the liveness probe, which separates exit from zombie."""

    @requires_proc_stat
    def test_a_running_child_with_a_space_in_its_name_is_alive(self) -> None:
        """A live child with a spaced ``comm`` is alive; its zombie is not."""
        # ``/proc/self/comm`` is the only way to give a process a name that
        # contains spaces; writing it avoids loading libc through FFI.
        script = (
            "import pathlib, time;"
            "pathlib.Path('/proc/self/comm').write_bytes(b'my worker');"
            "time.sleep(30)"
        )
        proc = subprocess.Popen(  # ruff:ignore[subprocess-without-shell-equals-true]
            [sys.executable, "-c", script]
        )
        try:
            stat_path = Path(f"/proc/{proc.pid}/stat")
            named = False
            ready_by = time.monotonic() + 5.0
            while time.monotonic() < ready_by:
                named = "my worker" in stat_path.read_text(encoding="utf-8")
                if named:
                    break
                time.sleep(0.02)
            assert named, "the child must have renamed itself before the probe runs"
            assert _is_alive(proc.pid), "a running child must count as alive"

            proc.kill()
            gone_by = time.monotonic() + 5.0
            while _is_alive(proc.pid) and time.monotonic() < gone_by:
                time.sleep(0.02)
            assert not _is_alive(proc.pid), (
                "an exited but unreaped child must not count as alive"
            )
        finally:
            proc.kill()
            proc.wait()
