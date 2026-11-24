"""Build execution and output capture.

Runs build targets and captures, sanitises, and truncates their output. The
target-selection policy and the result types live in `quality_policy`; this
module is the adapter that turns subprocess results into those types.
"""

from __future__ import annotations

import re
import typing as typ

from post_turn_quality_stop_hook.process import run
from post_turn_quality_stop_hook.quality_policy import (
    BuildTargetRequest,
    CommandResult,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.driver import BuildDriver

# Matches ANSI CSI sequences, OSC strings (BEL or ST terminated), DCS/SOS/PM/
# APC strings (ST terminated), and other short ESC sequences such as charset
# designations. Bodies exclude ESC so a match stops at its first terminator
# rather than swallowing visible text up to a later one; BEL terminates only
# OSC, so it may appear inside other control strings.
ANSI_ESCAPE = re.compile(
    r"\x1B(?:"
    r"\[[0-?]*[ -/]*(?:[@-~]|\Z)"  # CSI: colour, cursor, erase
    r"|\][^\x07\x1B]*(?:\x07|\x1B\\|\Z)"  # OSC: title, hyperlink
    r"|[PX^_][^\x1B]*(?:\x1B\\|\Z)"  # DCS, SOS, PM, APC
    r"|[ -/]*[0-~]"  # other ESC sequences, e.g. ESC ( B
    r")"
)


def truncate(text: str, max_chars: int) -> str:
    """Truncate text to a maximum length.

    Parameters
    ----------
    text
        Text to truncate.
    max_chars
        Maximum number of characters to keep.

    Returns
    -------
    str
        Truncated text with a placeholder if needed.

    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "\n... (output truncated) ...\n"
    if max_chars <= len(marker):
        return text[:max_chars]
    remaining = max_chars - len(marker)
    head = remaining // 2
    tail = remaining - head
    return text[:head] + marker + text[-tail:]


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from captured command output.

    ``NO_COLOR`` only asks a tool to avoid colour; a recipe can still emit
    escapes. Stripping at capture keeps the blocking reason readable and stops
    escape bytes from consuming the output budget.

    Parameters
    ----------
    text
        Captured standard output or standard error.

    Returns
    -------
    str
        Text with CSI, OSC, DCS/SOS/PM/APC, and short ESC sequences removed.

    """
    return ANSI_ESCAPE.sub("", text)


def run_build_targets(
    repo: Path,
    request: BuildTargetRequest,
    max_out: int,
) -> CommandResult:
    """Run build targets and capture output.

    Parameters
    ----------
    repo
        Repository root path.
    request
        Grouped build-target request.
    max_out
        Maximum number of output characters to capture.

    Returns
    -------
    CommandResult
        Immutable record of execution metadata and captured, sanitized output.

    """
    if not request.targets:
        return CommandResult(
            kind=request.kind,
            cmd="",
            exit_code=0,
            stdout="",
            stderr="",
        )

    cmd = build_command(request.driver, request.targets)
    try:
        p = run(cmd, repo)
    except FileNotFoundError as exc:
        return CommandResult(
            kind=request.kind,
            cmd=" ".join(cmd),
            exit_code=127,
            stdout="",
            stderr=f"{request.driver.executable} not found on PATH: {exc}",
        )
    return CommandResult(
        kind=request.kind,
        cmd=" ".join(cmd),
        exit_code=int(p.returncode),
        stdout=truncate(strip_ansi(p.stdout), max_out),
        stderr=truncate(strip_ansi(p.stderr), max_out),
    )


def build_command(driver: BuildDriver, targets: list[str]) -> list[str]:
    """Return the command used to build targets with a driver."""
    if driver.name == "netsuke":
        return [driver.executable, "build", *targets]
    return [driver.executable, "--no-print-directory", *targets]
