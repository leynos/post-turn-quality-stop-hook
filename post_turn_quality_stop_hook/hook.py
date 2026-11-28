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
import functools
import json
import os
import sys
import typing as typ
from pathlib import Path

import cyclopts
from cyclopts.exceptions import CycloptsError

from post_turn_quality_stop_hook.config import (
    DEFAULT_MAX_CONSECUTIVE_FAILURES,
    Config,
    ConfigError,
    load_config,
)
from post_turn_quality_stop_hook.failure_streak import run_with_failure_limit
from post_turn_quality_stop_hook.git import repo_root
from post_turn_quality_stop_hook.pipeline import run_stop_checks
from post_turn_quality_stop_hook.state import StopCheckOptions
from post_turn_quality_stop_hook.upstream import GitUpstreamHeadSource

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook.git_facts import GitFacts
    from post_turn_quality_stop_hook.pipeline import UpstreamSourceFactory
    from post_turn_quality_stop_hook.upstream_port import UpstreamHeadSource

if typ.TYPE_CHECKING:
    import collections.abc as cabc


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


def parse_env(
    config: Config | None = None,
    *,
    environ: cabc.Mapping[str, str] | None = None,
) -> tuple[str, StopCheckOptions]:
    """Read stop-hook configuration from an environment mapping.

    Parameters
    ----------
    config
        Merged configuration file values, or defaults when omitted.
    environ
        Environment mapping to read. Defaults to ``os.environ`` when ``None``,
        which keeps the process environment untouched but still overridable
        for isolated tests.

    Returns
    -------
    tuple[str, StopCheckOptions]
        Base ref string and runtime options.

    """
    env = os.environ if environ is None else environ
    return env.get("POST_TURN_BASE_REF", "origin/main"), StopCheckOptions(
        always_fetch=parse_bool_env(env.get("POST_TURN_ALWAYS_FETCH", "")),
        max_out=parse_max_output(
            env.get("POST_TURN_MAX_OUTPUT_CHARS", "12000"),
        ),
        config=config or Config(),
        build_driver=env.get("POST_TURN_BUILD_DRIVER", "auto"),
        netsuke_bin=env.get("POST_TURN_NETSUKE_BIN", "netsuke"),
        make_bin=env.get("POST_TURN_MAKE_BIN", "make"),
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

    Every decision after the working directory is known — including a
    configuration error — passes through the consecutive-failure breaker, so
    a block the agent cannot clear does not trap it in an endless loop.

    Returns
    -------
    int
        Exit code for the hook.

    """
    cli_options = parse_cli_args()
    start_cwd = resolve_start_cwd(parse_hook_input())
    if isinstance(cli_options, ConfigError):
        config: Config | ConfigError = cli_options
    else:
        config = load_runtime_config(start_cwd, override=cli_options.config)
    limit = (
        config.max_consecutive_failures
        if isinstance(config, Config)
        else DEFAULT_MAX_CONSECUTIVE_FAILURES
    )
    return run_with_failure_limit(
        start_cwd, limit, functools.partial(_run_configured_checks, start_cwd, config)
    )


def _run_configured_checks(start_cwd: Path, config: Config | ConfigError) -> int:
    """Block on an unusable configuration, or run the stop checks."""
    if isinstance(config, ConfigError):
        payload = {"decision": "block", "reason": str(config)}
        print(json.dumps(payload))
        return 0
    base_ref, options = parse_env(config)
    return run_stop_checks(
        start_cwd, base_ref, options, _upstream_source_factory(config)
    )


def _upstream_source_factory(config: Config) -> UpstreamSourceFactory:
    """Bind the upstream settings the port factory reads at call time."""
    return functools.partial(_git_upstream_source, config)


def _git_upstream_source(
    config: Config, repo: Path, facts: GitFacts | None
) -> UpstreamHeadSource:
    """Build the Git upstream port from the configured refresh settings."""
    return GitUpstreamHeadSource(
        repo,
        facts,
        refresh_upstream=config.unpushed_commits_refresh_upstream,
        fetch_timeout_seconds=config.unpushed_commits_fetch_timeout_seconds,
    )


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
