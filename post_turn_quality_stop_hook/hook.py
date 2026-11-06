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

import contextlib
import dataclasses
import json
import os
import sys
import typing as typ
from pathlib import Path

import cyclopts
from cyclopts.exceptions import CycloptsError

from post_turn_quality_stop_hook.config import Config, ConfigError, load_config
from post_turn_quality_stop_hook.decision_log import (
    DecisionLog,
    make_decision_log,
    settings_from_env,
)
from post_turn_quality_stop_hook.git import repo_root
from post_turn_quality_stop_hook.pipeline import run_stop_checks
from post_turn_quality_stop_hook.state import StopCheckOptions


class HookInput(typ.TypedDict, total=False):
    """Typed hook input from Claude Code's stdin JSON."""

    cwd: str
    project_dir: str
    session_id: str
    transcript_path: str
    hook_event_name: str
    stop_hook_active: bool


@dataclasses.dataclass(slots=True, frozen=True)
class CliOptions:
    """Command-line options for the stop hook."""

    config: Path | None = None


TRUTHY_VALUES = {"1", "true", "yes", "on"}


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
        for key in ("cwd", "project_dir", "session_id", "transcript_path"):
            value = hook_input.get(key)
            if isinstance(value, str):
                result[key] = value
        event_name = hook_input.get("hook_event_name")
        if isinstance(event_name, str):
            result["hook_event_name"] = event_name
        stop_hook_active = hook_input.get("stop_hook_active")
        if isinstance(stop_hook_active, bool):
            result["stop_hook_active"] = stop_hook_active
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
        """Return the CliOptions carrying the parsed --config path."""
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
    hook_input = parse_hook_input()
    start_cwd = resolve_start_cwd(hook_input)
    cli_options = parse_cli_args()
    override = cli_options.config if isinstance(cli_options, CliOptions) else None
    config = load_runtime_config(start_cwd, override=override)

    base_ref, options = parse_env(config if isinstance(config, Config) else None)
    try:
        decision_log = make_decision_log(
            settings_from_env(config if isinstance(config, Config) else None),
            correlation=_correlation(hook_input),
            start_dir=start_cwd,
            base_ref=base_ref,
        )
    except Exception:  # ruff:ignore[BLE001]  # logging must never change the decision.
        _warn_log_unavailable()
        decision_log = None

    exit_code = 0
    try:
        with _signal_guard(decision_log):
            if isinstance(cli_options, ConfigError):
                _emit_early_block(decision_log, cli_options)
                return 0
            if isinstance(config, ConfigError):
                _emit_early_block(decision_log, config)
                return 0
            exit_code = run_stop_checks(
                start_cwd,
                base_ref,
                options,
                decision_log=decision_log,
            )
    finally:
        _finish_log(decision_log, exit_code)
    return exit_code


def _warn_log_unavailable() -> None:
    """Warn on standard error that the decision log could not be created."""
    try:
        print(
            "post-turn-quality-stop-hook: could not create the decision log",
            file=sys.stderr,
        )
    except OSError:
        return


def _correlation(hook_input: HookInput) -> dict[str, object]:
    """Return correlation fields copied from the hook input."""
    return {
        "session_id": hook_input.get("session_id"),
        "transcript_path": hook_input.get("transcript_path"),
        "hook_event_name": hook_input.get("hook_event_name"),
        "stop_hook_active": hook_input.get("stop_hook_active"),
    }


def _emit_early_block(decision_log: DecisionLog | None, error: ConfigError) -> None:
    """Record and print a block decision raised before the pipeline runs."""
    if decision_log is not None:
        # The message may name a configuration path, including an
        # environment-provided XDG directory, so the log gets a fixed reason.
        decision_log.mark_block("invalid command line or configuration")
    print(json.dumps({"decision": "block", "reason": str(error)}))


@contextlib.contextmanager
def _signal_guard(
    decision_log: DecisionLog | None,
) -> typ.Iterator[None]:
    """Install best-effort signal handlers for the duration of the run."""
    if decision_log is None:
        yield
        return
    with decision_log.catch_signals():
        yield


def _finish_log(decision_log: DecisionLog | None, exit_code: int) -> None:
    """Write the finish record, recording an unexpected exception if present."""
    if decision_log is None:
        return
    error = sys.exc_info()[1]
    decision_log.finish(
        decision="error" if error is not None else None,
        exit_code=1 if error is not None else exit_code,
        error=_exception_text(error),
    )


def _exception_text(error: BaseException | None) -> str | None:
    """Return an exception's type for the decision log, without its message."""
    if error is None:
        return None
    # A message can carry a path, an environment value, or command output, so
    # the log records only the class name.
    return type(error).__name__


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
