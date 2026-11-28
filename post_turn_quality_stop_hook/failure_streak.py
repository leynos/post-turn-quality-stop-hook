"""Consecutive-failure circuit breaker for the stop hook.

A blocking stop hook sends the agent back to work. When the agent cannot fix
the failure — a flaky remote, a broken toolchain, a gate it has no authority
to satisfy — the hook and agent loop indefinitely. This module counts
consecutive blocking decisions per working directory and, once the configured
limit is reached, lets the next stop through without running the checks.

The count lives under ``$XDG_STATE_HOME/post-turn-quality-stop-hook/`` because
it is machine-local, non-essential state that must survive between hook
processes but does not belong in a repository or the user's configuration.

Bookkeeping fails open: an unreadable or unwritable state directory never
changes the decision the checks reach, it only disables the breaker.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import os
import sys
import tempfile
import typing as typ
from pathlib import Path

from post_turn_quality_stop_hook.config import is_non_negative_int

if typ.TYPE_CHECKING:
    import collections.abc as cabc

logger = logging.getLogger(__name__)

XDG_STATE_SUBPATH = Path("post-turn-quality-stop-hook") / "failure-streaks"


def state_root(environ: cabc.Mapping[str, str] | None = None) -> Path:
    """Return the directory that holds per-directory failure streaks.

    The XDG Base Directory specification requires implementations to ignore
    an empty or relative ``XDG_STATE_HOME`` and fall back to
    ``$HOME/.local/state``.
    """
    env = os.environ if environ is None else environ
    state_home = env.get("XDG_STATE_HOME", "")
    if state_home and Path(state_home).is_absolute():
        return Path(state_home) / XDG_STATE_SUBPATH
    return Path.home() / ".local" / "state" / XDG_STATE_SUBPATH


def streak_path(directory: Path, root: Path) -> Path:
    """Return the streak file for ``directory``.

    Hashing the path keeps file names short and free of separators while
    staying stable for the same directory across invocations.
    """
    digest = hashlib.sha256(str(directory).encode()).hexdigest()
    return root / f"{digest}.json"


def read_streak(path: Path) -> int:
    """Return the recorded consecutive-failure count, or 0 when unavailable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable failure streak %s: %s", path, exc)
        return 0
    count = data.get("consecutive_failures") if isinstance(data, dict) else None
    if not is_non_negative_int(count):
        return 0
    return count


def write_streak(path: Path, directory: Path, count: int) -> None:
    """Atomically record ``count`` consecutive failures for ``directory``.

    Writing to a sibling temporary file and renaming it means a concurrent
    reader sees either the old or the new count, never a torn file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"directory": str(directory), "consecutive_failures": count})
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as handle:
        handle.write(payload)
    temporary = Path(handle.name)
    try:
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def is_block_output(output: str) -> bool:
    """Return whether hook output contains a blocking decision."""
    for line in output.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("decision") == "block":
            return True
    return False


def _short_circuit_message(limit: int) -> str:
    """Return the JSON payload that allows the stop and explains why."""
    message = (
        f"post-turn-quality-stop-hook blocked {limit} consecutive times in this "
        "directory, so this stop was allowed without running the checks. "
        "Checks resume on the next stop."
    )
    return json.dumps({"systemMessage": message})


def _record_outcome(path: Path, directory: Path, count: int) -> None:
    """Persist the new streak, clearing it when ``count`` is zero."""
    try:
        if count:
            write_streak(path, directory, count)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not record failure streak %s: %s", path, exc)


def run_with_failure_limit(
    directory: Path,
    limit: int,
    run: cabc.Callable[[], int],
    *,
    state_dir: Path | None = None,
) -> int:
    """Run the stop checks unless ``directory`` has blocked ``limit`` times.

    Parameters
    ----------
    directory
        Working directory that identifies the streak.
    limit
        Consecutive blocks tolerated before the next stop is let through.
        Zero disables the breaker.
    run
        Stop checks to run. They report decisions by printing hook JSON to
        standard output and return the hook exit code.
    state_dir
        Directory holding streak files. Defaults to the XDG state location.

    Returns
    -------
    int
        Exit code from ``run``, or ``0`` when the breaker short-circuits.

    """
    if limit <= 0:
        return run()

    path = streak_path(directory, state_dir or state_root())
    previous = read_streak(path)
    if previous >= limit:
        # Reset so that checks resume on the next stop instead of leaving the
        # hook permanently disabled for this directory.
        _record_outcome(path, directory, 0)
        print(_short_circuit_message(previous))
        return 0

    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            exit_code = run()
    finally:
        # Forward whatever the checks printed even if they raise, so the
        # breaker never hides output the hook would otherwise have emitted.
        sys.stdout.write(buffer.getvalue())
    output = buffer.getvalue()

    _record_outcome(path, directory, previous + 1 if is_block_output(output) else 0)
    return exit_code
