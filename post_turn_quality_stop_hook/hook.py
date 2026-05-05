#!/usr/bin/env python3
"""Claude Code Stop-hook quality gate — CLI entry point.

Reads JSON hook input from stdin, resolves the working directory,
parses environment variables, and delegates to the full quality-gate
pipeline in `post_turn_quality_stop_hook.state`.

Behaviour knobs (env vars):
- ``POST_TURN_ALWAYS_FETCH`` — always fetch ``origin/main``.
- ``POST_TURN_BASE_REF`` — override the base ref for diffing.
- ``POST_TURN_MAX_OUTPUT_CHARS`` — truncate per-command output.
- ``POST_TURN_COMPUSH`` — block if local work is unpublished.
- ``POST_TURN_BUILD_DRIVER`` — select the build driver: ``auto``
  (default), ``netsuke``, or ``make``. When ``netsuke`` is selected
  the hook invokes Netsuke build steps instead of Makefile targets.

Examples
--------
Run the hook manually with a default environment:

    POST_TURN_ALWAYS_FETCH=1 post-turn-quality-stop-hook < /dev/null

"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from post_turn_quality_stop_hook.pipeline import run_stop_checks
from post_turn_quality_stop_hook.state import StopCheckOptions

TRUTHY_VALUES = {"1", "true", "yes"}


def parse_bool_env(value: str) -> bool:
    """Parse a boolean environment value.

    Parameters
    ----------
    value
        Raw environment value.

    Returns
    -------
    bool
        True when the value is a recognized truthy token.

    """
    return value.strip().lower() in TRUTHY_VALUES


def parse_max_output(value: str, default: int = 12000) -> int:
    """Parse the max output character limit.

    Parameters
    ----------
    value
        Raw environment value.
    default
        Fallback value when parsing fails.

    Returns
    -------
    int
        Maximum number of output characters to capture.

    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def parse_env() -> tuple[str, StopCheckOptions]:
    """Read stop-hook configuration from environment variables.

    Returns
    -------
    tuple[str, StopCheckOptions]
        Base ref string and runtime options.

    """
    return os.environ.get("POST_TURN_BASE_REF", "origin/main"), StopCheckOptions(
        always_fetch=parse_bool_env(os.environ.get("POST_TURN_ALWAYS_FETCH", "")),
        max_out=parse_max_output(
            os.environ.get("POST_TURN_MAX_OUTPUT_CHARS", "12000"),
        ),
        compush=parse_bool_env(os.environ.get("POST_TURN_COMPUSH", "")),
        build_driver=os.environ.get("POST_TURN_BUILD_DRIVER", "auto"),
        netsuke_bin=os.environ.get("POST_TURN_NETSUKE_BIN", "netsuke"),
        make_bin=os.environ.get("POST_TURN_MAKE_BIN", "make"),
    )


def parse_hook_input() -> dict[str, object]:
    """Parse JSON hook input from stdin.

    Returns
    -------
    dict[str, object]
        Parsed hook input as a dict (empty if missing or invalid).

    """
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return {}
    match hook_input:
        case dict() as data:
            return data
        case _:
            return {}


def resolve_start_cwd(hook_input: dict[str, object]) -> Path:
    """Resolve the start working directory from hook-input.

    Parameters
    ----------
    hook_input
        Parsed hook input from stdin.

    Returns
    -------
    Path
        Absolute path to the working directory.

    """
    cwd = hook_input.get(
        "cwd",
        hook_input.get("project_dir"),
    )
    if cwd and isinstance(cwd, str):
        return Path(cwd).resolve()
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd())).resolve()


def main() -> int:
    """Run the stop-hook checks.

    Returns
    -------
    int
        Exit code for the hook.

    """
    hook_input = parse_hook_input()
    start_cwd = resolve_start_cwd(hook_input)
    base_ref, options = parse_env()
    return run_stop_checks(start_cwd, base_ref, options)


if __name__ == "__main__":
    raise SystemExit(main())
