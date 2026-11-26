"""Subprocess execution with bounded timeout and process-group cleanup.

Commands such as ``git fetch`` can spawn transports that outlive the direct
child, so a timed-out command must be stopped as a process group. This module
owns that supervision, keeping the signalling side effects in one place.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import time
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc


#: Status recorded for a command that could not be started. A real status is
#: either a wait status in 0-255 or the negative signal number that killed the
#: child, so this value cannot be mistaken for either. A record that carries it
#: says "no child answered", which is different from a child that failed.
LAUNCH_FAILURE_STATUS = -256


def _subprocess_env(
    unset: typ.Iterable[str] = (),
    *,
    environ: cabc.Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a sanitized environment for subprocess execution.

    The hook may run under a restricted PATH (for example, when launched
    as a Claude Code hook).  Prepend standard user-local tool directories
    so that ``make`` and ``netsuke`` can find ``ruff``, ``ty``,
    ``markdownlint-cli2``, and similar quality tools.

    Remove ``FORCE_COLOR`` and ``CLICOLOR_FORCE`` and set ``NO_COLOR`` to
    ``"1"`` to request plain output; a tool may ignore the request, so
    ``run_build_targets`` also strips escape sequences before capture. ANSI
    escapes would otherwise corrupt commands that print paths.

    Parameters
    ----------
    unset
        Names to remove from the supplied environment. A caller whose
        command must not inherit a process-global override (for example, a
        ``GIT_INDEX_FILE`` naming another repository's index) drops it here
        rather than mutating ``os.environ``.
    environ
        Environment to start from. ``None`` reads ``os.environ``, which is the
        composition-free default; a caller that already snapshotted the process
        environment passes it explicitly so its commands see one consistent
        view.

    Returns
    -------
    dict[str, str]
        Environment mapping with an extended ``PATH`` and colour output
        disabled.

    """
    env = os.environ.copy() if environ is None else dict(environ)
    for name in unset:
        env.pop(name, None)
    home = Path.home()
    extra_dirs = [
        home / ".local" / "bin",
        home / ".bun" / "bin",
        home / ".cargo" / "bin",
        home / ".lody" / "bin",
        home / "go" / "bin",
    ]
    existing_path = env.get("PATH", "")
    entries = existing_path.split(os.pathsep)
    for d in extra_dirs:
        d_str = str(d)
        if d.is_dir() and d_str not in entries:
            entries.insert(0, d_str)
    env["PATH"] = os.pathsep.join(entries)
    # Colour codes corrupt captured output, and tools that print paths can
    # turn a coloured path into part of a filesystem path.
    env.pop("FORCE_COLOR", None)
    env.pop("CLICOLOR_FORCE", None)
    env["NO_COLOR"] = "1"
    return env


#: Seconds a timed-out command is given to exit after ``SIGTERM`` before the
#: whole process group is killed. Git writes ref locks while fetching, so an
#: immediate ``SIGKILL`` can leave a lock behind that blocks later fetches.
_TERMINATION_GRACE_SECONDS = 2.0

#: Process-group signalling is POSIX-only. Windows has neither ``os.getpgid``
#: nor ``os.killpg``, so the direct child is signalled there instead.
_SUPPORTS_PROCESS_GROUPS = hasattr(os, "getpgid") and hasattr(os, "killpg")

#: Decoding for captured output. Git can write bytes that are not valid UTF-8
#: to standard error, for example when a transport fails mid-write. Decoding
#: with replacement keeps a diagnostic available instead of letting a
#: ``UnicodeDecodeError`` abort the hook path that was meant to report it.
_OUTPUT_ENCODING = "utf-8"


def _process_group_id(proc: subprocess.Popen[str]) -> int | None:
    """Return the process group to signal, or ``None`` if it has gone."""
    if not _SUPPORTS_PROCESS_GROUPS:
        return None
    try:
        return os.getpgid(proc.pid)
    except ProcessLookupError, PermissionError:
        return None


def _signal_process(
    proc: subprocess.Popen[str], pgid: int | None, sig: signal.Signals
) -> None:
    """Signal a process group, falling back to the direct child.

    ``subprocess.run`` only signals the direct child. A ``git fetch`` over SSH
    leaves the SSH transport running in the same process group, so the whole
    group is signalled first. The fallback covers a group that vanished before
    it could be signalled, and a child that already exited is ignored.
    """
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError, PermissionError:
            pass
        else:
            return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        proc.send_signal(sig)


def _wait_for_exit(proc: subprocess.Popen[str], timeout: float) -> bool:
    """Wait for a process to exit, returning whether it did so in time."""
    try:
        proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _close_streams(proc: subprocess.Popen[str]) -> None:
    """Close captured pipes once the child has exited.

    A child that survived both termination signals may still have a reader
    thread blocked on its pipe. Closing the buffered stream then waits for that
    thread's lock without a timeout, which would hang the hook instead of
    reporting the timeout. Leaving a live child's streams to process teardown
    is bounded: the hook exits immediately after reporting the timeout.
    """
    if proc.poll() is None:
        return
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def _process_group_alive(pgid: int | None) -> bool:
    """Return whether any process in a group still exists.

    Signal ``0`` performs the permission and existence checks without sending
    anything. A group outlives its leader while any member remains, which is
    exactly the case a timed-out transport leaves behind.
    """
    if pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_group_exit(pgid: int | None, timeout: float) -> bool:
    """Wait for every process in a group to exit, returning whether it did."""
    deadline = time.monotonic() + timeout
    while _process_group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        # The leader may already be reaped, so poll the group rather than the
        # child. The interval is short enough to observe a fast SIGKILL.
        time.sleep(_TERMINATION_GRACE_SECONDS / 40)
    return True


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    """Stop a timed-out command and any transport it started.

    ``SIGTERM`` comes first so Git can remove lock files and close transports
    cleanly. Only a command that ignores the grace period is killed outright.
    The direct child exiting is not enough: a descendant that ignores
    ``SIGTERM``, such as a transport or credential helper, would otherwise
    survive, so cleanup continues until the whole group is empty. Every wait is
    bounded, so a wedged process cannot stall the hook.
    """
    pgid = _process_group_id(proc)
    _signal_process(proc, pgid, signal.SIGTERM)
    child_exited = _wait_for_exit(proc, _TERMINATION_GRACE_SECONDS)
    if child_exited and _wait_for_group_exit(pgid, 0):
        return
    sigkill = getattr(signal, "SIGKILL", None)
    if sigkill is None:
        # Windows defines no SIGKILL; ``kill`` is the strongest stop available.
        proc.kill()
    else:
        # The group identifier stays valid while any member remains, so this
        # still reaches descendants after the leader has been reaped.
        _signal_process(proc, pgid, sigkill)
    if not child_exited:
        child_exited = _wait_for_exit(proc, _TERMINATION_GRACE_SECONDS)
    if child_exited and _wait_for_group_exit(pgid, 0):
        return
    if _wait_for_group_exit(pgid, _TERMINATION_GRACE_SECONDS):
        return
    _close_streams(proc)


def _run_with_timeout(  # ruff:ignore[too-many-arguments]  # command, directory, deadline, unset names and environment are distinct inputs.
    cmd: list[str],
    cwd: Path,
    timeout: float,
    unset: typ.Iterable[str] = (),
    *,
    environ: cabc.Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command in its own process group so a timeout kills transports."""
    proc = subprocess.Popen(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: command and args are controlled (no shell, no user-supplied command strings).
        cmd,
        cwd=str(cwd),
        text=True,
        encoding=_OUTPUT_ENCODING,
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_subprocess_env(unset, environ=environ),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,
            stdout="",
            stderr=f"command timed out after {timeout} seconds",
        )
    return subprocess.CompletedProcess(
        args=cmd, returncode=proc.returncode, stdout=stdout, stderr=stderr
    )


class LaunchedProcess(subprocess.CompletedProcess[str]):
    """A completed process that records whether the command actually ran.

    ``process.run`` answers a launch failure with a synthetic status so every
    caller can keep its ordinary error handling. A caller that reads status
    ``1`` as a command's own answer would then mistake "Git never started" for
    "Git ran and said no". This class keeps the two apart: ``launched`` is
    ``False`` only for the synthetic result, so a status alone is never
    ambiguous.
    """

    #: Whether the command was started. The synthetic results set it ``False``.
    launched: bool

    def __init__(  # ruff:ignore[too-many-arguments]  # mirrors the stdlib result it extends.
        self,
        args: list[str],
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        *,
        launched: bool = True,
    ) -> None:
        """Store the process result and whether the command actually started."""
        super().__init__(args, returncode, stdout, stderr)
        self.launched = launched


def _launch_failure(cmd: list[str], exc: OSError) -> LaunchedProcess:
    """Return the synthetic result for a command that could not be started."""
    return LaunchedProcess(
        args=cmd, returncode=1, stdout="", stderr=str(exc), launched=False
    )


def run(  # ruff:ignore[too-many-arguments]  # command, directory, deadline, unset names and environment are distinct inputs.
    cmd: list[str],
    cwd: Path,
    *,
    timeout: float | None = None,
    unset: typ.Iterable[str] = (),
    environ: cabc.Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command in the given working directory.

    Parameters
    ----------
    cmd
        Command and arguments to run.
    cwd
        Working directory for the subprocess.
    timeout
        Optional timeout in seconds. With a timeout the command runs in its
        own session, and a command that exceeds the deadline is signalled as a
        whole process group: ``SIGTERM`` first, then ``SIGKILL`` after a short
        grace period. This stops transports such as SSH rather than leaving
        them running. A timed-out command is reported as a failed process with
        return code ``124`` so callers can apply their normal error handling.
    unset
        Names removed from the inherited environment for this command only.
        A query that must read the repository it inspects drops an inherited
        ``GIT_INDEX_FILE`` here instead of changing the environment every
        other command runs with.
    environ
        Environment to start from. ``None`` reads ``os.environ``, which keeps a
        direct caller working without a composition step. A caller that has
        already snapshotted the process environment passes it, so its commands
        all see the same view even if the process environment changes.

    Returns
    -------
    subprocess.CompletedProcess[str]
        Completed process with captured output. A command that could not be
        started is reported as a :class:`LaunchedProcess` whose ``launched`` is
        ``False``, so a caller can tell a launch failure from a status the
        command itself returned.

    """
    try:
        if timeout is not None:
            return _run_with_timeout(cmd, cwd, timeout, unset, environ=environ)
        completed = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: command and args are controlled (no shell, no user-supplied command strings).
            cmd,
            cwd=str(cwd),
            text=True,
            encoding=_OUTPUT_ENCODING,
            errors="replace",
            capture_output=True,
            check=False,
            env=_subprocess_env(unset, environ=environ),
        )
        return LaunchedProcess(
            args=completed.args,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except FileNotFoundError as exc:
        if Path(exc.filename or "") != cwd:
            raise
        return _launch_failure(cmd, exc)
    except NotADirectoryError as exc:
        return _launch_failure(cmd, exc)
