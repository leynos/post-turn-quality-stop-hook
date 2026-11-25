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
- ``POST_TURN_LOG`` — enable or disable the durable decision log.
- ``POST_TURN_LOG_RETENTION_DAYS`` — days of daily log files to keep.

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

from post_turn_quality_stop_hook.config import Config, ConfigError, load_config
from post_turn_quality_stop_hook.decision_log.clock import SystemClock
from post_turn_quality_stop_hook.fetch_observation import ObserverFetchObserver
from post_turn_quality_stop_hook.git import repo_root
from post_turn_quality_stop_hook.hook_composition import (
    DecisionLogRun,
    ProcessSnapshot,
    decision_log_run,
)
from post_turn_quality_stop_hook.pipeline import run_stop_checks
from post_turn_quality_stop_hook.recording import InvocationLog
from post_turn_quality_stop_hook.repository_facts_git import GitRepositoryFactsSource
from post_turn_quality_stop_hook.state import StopCheckOptions
from post_turn_quality_stop_hook.upstream import GitUpstreamHeadSource

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from post_turn_quality_stop_hook.decision_log.clock import Clock
    from post_turn_quality_stop_hook.git_facts import GitFacts
    from post_turn_quality_stop_hook.observer import PipelineObserver
    from post_turn_quality_stop_hook.pipeline import UpstreamSourceFactory
    from post_turn_quality_stop_hook.upstream_port import UpstreamHeadSource


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


def resolve_start_cwd(hook_input: HookInput, fallback: Path) -> Path:
    """Resolve the start working directory from hook-input.

    The payload's ``cwd`` and ``project_dir`` are read first. When neither is
    present the caller's ``fallback`` is used, which the composition edge
    supplies from the process snapshot it already read. This function
    therefore never consults ``os.environ`` or ``Path.cwd`` itself, so a
    caller can decide both without patching process globals.

    Parameters
    ----------
    hook_input
        Parsed hook input from stdin.
    fallback
        Directory to use when the payload names none. The composition edge
        builds it from ``CLAUDE_PROJECT_DIR`` and the working directory.

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
    return fallback


def _configured_start_dir(environ: typ.Mapping[str, str]) -> Path | None:
    """Return ``CLAUDE_PROJECT_DIR`` when the environment names one."""
    configured = environ.get("CLAUDE_PROJECT_DIR")
    if not configured:
        return None
    try:
        return Path(configured).resolve()
    except OSError:
        return None


def _working_directory() -> Path | None:
    """Return the process working directory, or ``None`` when it is gone."""
    try:
        return Path.cwd()
    except OSError:
        return None


def _fallback_start_dir(environ: typ.Mapping[str, str]) -> Path:
    """Return the payload-free start directory, or the empty path.

    ``CLAUDE_PROJECT_DIR`` is consulted first, and ``Path.cwd`` is evaluated
    only when it is absent: a configured project directory is authoritative,
    so a removed working directory must not prevent the hook from using it. A
    failed read is answered with ``Path()``, because the result is only used
    when the payload names neither ``cwd`` nor ``project_dir``.
    """
    configured = _configured_start_dir(environ)
    if configured is not None:
        return configured
    return _working_directory() or Path()


def _log_start_dir(environ: typ.Mapping[str, str]) -> Path:
    """Return a start directory for the log's context, best effort.

    The log is created before standard input is read, so a removed working
    directory must not stop the hook here. ``CLAUDE_PROJECT_DIR`` is checked
    before the working directory is read, so a configured project directory is
    used even when the process has no readable working directory. The value is
    descriptive only; the authoritative directory is resolved from the payload
    afterwards.
    """
    return _fallback_start_dir(environ)


def main(*, clock: Clock | None = None) -> int:
    """Run the stop-hook checks.

    Parameters
    ----------
    clock
        Time source used to stamp the run's start time. The process clock is
        supplied when omitted; a test passes one to pin the reading.

    Returns
    -------
    int
        Exit code for the hook.

    """
    # Start the clock before any input or configuration work so the finish
    # record's total duration covers startup.
    started = (clock or SystemClock()).monotonic()
    # Read the process environment and home exactly once. Every later step --
    # the log's location, the base ref, and the hook options -- reads this
    # snapshot, so a value the process changes mid-run cannot split a single
    # invocation's settings in two.
    process = ProcessSnapshot.read()
    cli_options = parse_cli_args()
    if isinstance(cli_options, ConfigError):
        # An invalid command line is answered before standard input is read,
        # before repository discovery, and before the configuration load. The
        # payload is printed before the decision-log run opens, because opening
        # it may ask Git whether the daily file is tracked or ignored; the
        # response therefore never waits on those queries.
        return _early_cli_block(cli_options, started, process)

    # The log lifecycle owns creation, the signal guard, correlation and the
    # single terminal record. It is entered before standard input is read, so a
    # stalled or killed read still leaves a start record, and an exception that
    # propagates out of the body is recorded from the context manager rather
    # than from whatever exception an outer caller happens to be handling.
    with decision_log_run(
        start_dir=_log_start_dir(process.environ),
        base_ref=_base_ref_from_env(process.environ),
        started=started,
        process=process,
    ) as run:
        with run.stage("input"):
            hook_input = parse_hook_input()
        # The fallback comes from this edge, which already read the process
        # environment and can read the working directory once. Resolving it
        # here keeps ``resolve_start_cwd`` a pure function of its inputs.
        start_cwd = resolve_start_cwd(hook_input, _fallback_start_dir(process.environ))
        run.set_correlation(hook_input)

        config = load_runtime_config(start_cwd, override=cli_options.config)
        run.apply_config(config)

        base_ref, options = parse_env(
            config if isinstance(config, Config) else None,
            environ=process.environ,
        )
        if isinstance(config, ConfigError):
            run.mark_block("invalid command line or configuration")
            print(json.dumps({"decision": "block", "reason": str(config)}))
            return 0
        exit_code = run_stop_checks(
            start_cwd,
            base_ref,
            options,
            _upstream_source_factory(config),
            repository_facts_source=GitRepositoryFactsSource(),
            invocation=InvocationLog(recorder=run.recorder),
        )
        run.finish(exit_code)
    return exit_code


def _early_cli_block(
    error: ConfigError, started: float, process: ProcessSnapshot
) -> int:
    """Emit an invalid-argument block without reading stdin.

    The payload is printed and flushed before the decision-log run opens.
    Opening that run can ask Git whether the daily file is tracked or ignored
    when the state directory lies inside a worktree, so printing first keeps
    the invalid-argument response from waiting on any Git query. The run is
    still opened afterwards so the block is recorded like any other decision;
    nothing the record writes can change the payload or the exit code.
    """
    print(json.dumps({"decision": "block", "reason": str(error)}), flush=True)
    with decision_log_run(
        start_dir=_log_start_dir(process.environ),
        base_ref=_base_ref_from_env(process.environ),
        started=started,
        process=process,
    ) as run:
        _record_early_block(run)
    return 0


def _upstream_source_factory(config: Config) -> UpstreamSourceFactory:
    """Bind the upstream settings the port factory reads at call time."""
    return functools.partial(_git_upstream_source, config)


def _git_upstream_source(
    config: Config,
    repo: Path,
    facts: GitFacts | None,
    observer: PipelineObserver,
) -> UpstreamHeadSource:
    """Build the Git upstream port from the configured refresh settings.

    The port receives the run's observer, so the upstream refresh's own
    ``git fetch`` is recorded as a blocking command with its real exit code
    rather than running invisibly behind the gate.
    """
    return GitUpstreamHeadSource(
        repo,
        facts,
        refresh_upstream=config.unpushed_commits_refresh_upstream,
        fetch_timeout_seconds=config.unpushed_commits_fetch_timeout_seconds,
        observer=ObserverFetchObserver(observer),
    )


def _record_early_block(run: DecisionLogRun) -> None:
    """Record a block decision raised before the pipeline runs.

    The payload has already been printed by :func:`_early_cli_block`. The
    parse error's message may name a configuration path, including an
    environment-provided XDG directory, so the log gets a fixed reason
    instead.
    """
    run.mark_block("invalid command line or configuration")


def _base_ref_from_env(environ: typ.Mapping[str, str]) -> str:
    """Return the configured base ref from an environment snapshot."""
    return environ.get("POST_TURN_BASE_REF", "origin/main")


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
