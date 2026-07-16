#!/usr/bin/env python3
"""Claude Code Stop-hook quality gate — CLI entry point.

Reads JSON hook input from stdin, resolves the working directory,
parses environment variables, and delegates to the full quality-gate
pipeline in `post_turn_quality_stop_hook.pipeline`.

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

import dataclasses
import json
import os
import sys
import typing as typ
from pathlib import Path

import cyclopts
from cyclopts.exceptions import CycloptsError

from post_turn_quality_stop_hook.config import Config, ConfigError, load_config
from post_turn_quality_stop_hook.git import repo_root
from post_turn_quality_stop_hook.pipeline import run_stop_checks
from post_turn_quality_stop_hook.state import StopCheckOptions


class HookInput(typ.TypedDict, total=False):
    """Typed hook input from Claude Code's stdin JSON."""

    cwd: str
    project_dir: str


@dataclasses.dataclass(slots=True, frozen=True)
class CliOptions:
    """Command-line options for the stop hook."""

    config: Path | None = None


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
    except (TypeError, ValueError):  # fmt: skip
        return default
    if parsed < 0:
        return default
    return parsed


def parse_env(config: Config | None = None) -> tuple[str, StopCheckOptions]:
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
        config=config or Config(),
        build_driver=os.environ.get("POST_TURN_BUILD_DRIVER", "auto"),
        netsuke_bin=os.environ.get("POST_TURN_NETSUKE_BIN", "netsuke"),
        make_bin=os.environ.get("POST_TURN_MAKE_BIN", "make"),
    )


def parse_hook_input() -> HookInput:
    """Parse JSON hook input from stdin.

    Returns
    -------
    HookInput
        Typed hook input (empty if missing or invalid).

    """
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):  # fmt: skip
        return HookInput()
    if isinstance(hook_input, dict):
        result: HookInput = {}
        cwd = hook_input.get("cwd")
        if isinstance(cwd, str):
            result["cwd"] = cwd
        project_dir = hook_input.get("project_dir")
        if isinstance(project_dir, str):
            result["project_dir"] = project_dir
        return result
    return HookInput()


def parse_cli_args(argv: list[str] | None = None) -> CliOptions | ConfigError:
    """Parse command-line options.

    Parameters
    ----------
    argv
        Optional argument vector without the executable name.

    Returns
    -------
    CliOptions | ConfigError
        Parsed command-line options, or an error suitable for hook output.

    """

    def command(*, config: Path | None = None) -> CliOptions:
        return CliOptions(config=config)

    app = cyclopts.App(default_command=command)
    try:
        command_func, bound, _ignored = app.parse_args(
            sys.argv[1:] if argv is None else argv,
            exit_on_error=False,
            print_error=False,
        )
    except CycloptsError as exc:
        return ConfigError(f"Invalid command line: {exc}")

    # typ.cast's first argument is inert at runtime, and bound.args is always
    # empty for the keyword-only default command (equivalent mutants).
    return typ.cast(
        "CliOptions",  # pragma: no mutate
        command_func(*bound.args, **bound.kwargs),  # pragma: no mutate
    )


def resolve_start_cwd(hook_input: HookInput) -> Path:
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
    cwd = hook_input.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return Path(cwd).resolve()
    project_dir = hook_input.get("project_dir")
    if isinstance(project_dir, str) and project_dir.strip():
        return Path(project_dir).resolve()
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd())).resolve()


def main() -> int:
    """Run the stop-hook checks.

    Returns
    -------
    int
        Exit code for the hook.

    """
    cli_options = parse_cli_args()
    if isinstance(cli_options, ConfigError):
        payload = {"decision": "block", "reason": str(cli_options)}
        print(json.dumps(payload))
        return 0

    hook_input = parse_hook_input()
    start_cwd = resolve_start_cwd(hook_input)
    config = load_runtime_config(start_cwd, override=cli_options.config)
    if isinstance(config, ConfigError):
        payload = {"decision": "block", "reason": str(config)}
        print(json.dumps(payload))
        return 0

    base_ref, options = parse_env(config)
    return run_stop_checks(start_cwd, base_ref, options)


def load_runtime_config(
    start_cwd: Path, *, override: Path | None = None
) -> Config | ConfigError:
    """Load repository config when ``start_cwd`` is inside a Git worktree."""
    repo, _err = repo_root(start_cwd)
    if repo is None:
        return Config()
    try:
        return load_config(repo, override=override)
    except ConfigError as exc:
        return exc


if __name__ == "__main__":
    raise SystemExit(main())
