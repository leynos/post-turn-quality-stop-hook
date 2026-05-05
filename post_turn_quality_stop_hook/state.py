"""Shared data structures and formatting utilities.

Defines the hook execution state, preparation results,
runtime options, formatting helpers, failure reporting,
and top-level orchestration (prepare, evaluate, run).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from post_turn_quality_stop_hook.driver import (
    BuildDriver,
    _is_executable_available,
    get_build_targets,
    select_build_driver,
)
from post_turn_quality_stop_hook.execution import (
    BLOCKED_STATUS,
    CODE_CATS,
    MAX_CHANGED_FILES_IN_REASON,
    MD_CATS,
    BuildTargetRequest,
    CommandResult,
    run_build_targets,
    targets_for_categories,
)
from post_turn_quality_stop_hook.git import (
    changed_files,
    ensure_base_ref,
    get_upstream_ref,
    has_uncommitted_changes,
    has_unpushed_commits,
    merge_base,
    repo_root,
)

PY_TS_EXTS = {".py", ".pyi", ".ts", ".tsx", ".mts", ".cts"}

RUST_EXTS = {".rs"}

MD_EXTS = {".md", ".mdx", ".markdown"}

TRUTHY_VALUES = {"1", "true", "yes"}


def default_categories() -> dict[str, bool]:
    """Return a default category mapping.

    Returns
    -------
    dict[str, bool]
        Default mapping of category names to enabled flags.

    """
    return {"python_ts": False, "rust": False, "markdown": False}


@dataclasses.dataclass(slots=True)
class HookState:
    """Execution state for the stop hook.

    Attributes
    ----------
    ok
        Whether the hook checks succeeded.
    base_ref
        Base ref used for comparison.
    base_commit
        Resolved merge-base commit.
    changed_files
        Files changed relative to the base commit.
    categories
        Detected change categories.
    build_driver
        Build driver selected to run quality targets.
    targets_requested
        Build targets requested based on change categories.
    targets_run
        Build targets executed.
    targets_skipped
        Requested targets that were not present for the selected driver.
    commands
        Executed commands and their outputs.
    fetched
        Whether a fetch was performed.
    error
        Error message when blocking.

    """

    ok: bool = True
    base_ref: str = "origin/main"
    base_commit: str | None = None
    changed_files: list[str] = dataclasses.field(default_factory=list)
    categories: dict[str, bool] = dataclasses.field(default_factory=default_categories)
    build_driver: str | None = None
    targets_requested: list[str] = dataclasses.field(default_factory=list)
    targets_run: list[str] = dataclasses.field(default_factory=list)
    targets_skipped: list[str] = dataclasses.field(default_factory=list)
    commands: list[CommandResult] = dataclasses.field(default_factory=list)
    fetched: bool = False
    error: str | None = None


@dataclasses.dataclass(slots=True)
class RunStopChecksPreparation:
    """Prepared state for ``run_stop_checks``.

    Attributes
    ----------
    ok
        Whether preparation succeeded and execution should continue.
    exit_code
        Exit code to return immediately when preparation did not succeed.
    state
        Hook state populated during preparation.
    repo
        Resolved repository root when preparation succeeded.

    """

    ok: bool
    exit_code: int
    state: HookState
    repo: Path | None = None


@dataclasses.dataclass(slots=True, frozen=True)
class StopCheckOptions:
    """Runtime options for stop-hook checks.

    Attributes
    ----------
    always_fetch
        Whether to always fetch origin/main.
    max_out
        Maximum number of output characters to capture.
    compush
        Whether to remind the agent to commit and push when dirty.
    build_driver
        Requested build driver: ``auto``, ``netsuke``, or ``make``.
    netsuke_bin
        Executable used when running Netsuke.
    make_bin
        Executable used when running Make.

    """

    always_fetch: bool
    max_out: int
    compush: bool = False
    build_driver: str = "auto"
    netsuke_bin: str = "netsuke"
    make_bin: str = "make"


def detect_categories(files: list[str]) -> dict[str, bool]:
    """Detect change categories from a file list.

    Parameters
    ----------
    files
        List of file paths.

    Returns
    -------
    dict[str, bool]
        Mapping of category names to detection flags.

    """
    cats = default_categories()
    for f in files:
        ext = Path(f).suffix.lower()
        if ext in PY_TS_EXTS:
            cats["python_ts"] = True
        if ext in RUST_EXTS:
            cats["rust"] = True
        if ext in MD_EXTS:
            cats["markdown"] = True
    return cats


def _detected_category_labels(categories: dict[str, bool]) -> list[str]:
    """Return human-readable labels for detected change categories."""
    labels: list[str] = []
    if categories.get("python_ts"):
        labels.append("Python/TypeScript")
    if categories.get("rust"):
        labels.append("Rust")
    if categories.get("markdown"):
        labels.append("Markdown")
    return labels


def _format_changed_files(state: HookState) -> list[str]:
    """Format the changed-file section for a blocking reason."""
    base_ref = state.base_ref or "?"
    changed = state.changed_files
    lines = ["", f"Changed files vs {base_ref}: {len(changed)}"]
    lines.extend(f"- {path}" for path in changed[:MAX_CHANGED_FILES_IN_REASON])
    if len(changed) > MAX_CHANGED_FILES_IN_REASON:
        remaining = len(changed) - MAX_CHANGED_FILES_IN_REASON
        lines.append(f"- ... (+{remaining} more)")
    return lines


def _format_target_summary(state: HookState) -> list[str]:
    """Format requested, run, and skipped build targets."""
    lines: list[str] = []
    if state.build_driver:
        lines.extend(("", f"Build driver: {state.build_driver}"))
    if state.targets_requested:
        lines.extend((
            "",
            "Requested build targets: " + " ".join(state.targets_requested),
        ))
    if state.targets_run:
        lines.append("Targets run: " + " ".join(state.targets_run))
    if state.targets_skipped:
        lines.append("Targets skipped (missing): " + " ".join(state.targets_skipped))
    return lines


def _format_command_failure(command: CommandResult) -> list[str]:
    """Format one failed command for the blocking reason."""
    cmd = command.get("cmd", "")
    code = command.get("exit_code", "?")
    combined = "\n".join([
        x for x in [command.get("stdout", ""), command.get("stderr", "")] if x
    ]).strip()
    return [
        "",
        f"Command failed (exit {code}): {cmd}",
        "```",
        combined or "(no output captured)",
        "```",
    ]


def format_reason(state: HookState) -> str:
    """Format a blocking reason for hook output.

    Parameters
    ----------
    state
        Hook execution state.

    Returns
    -------
    str
        Human-readable reason string.

    """
    lines: list[str] = ["Post-turn checks failed."]

    if state.error:
        lines.extend(("", f"Error: {state.error}"))

    base_ref = state.base_ref or "?"
    base_commit = state.base_commit or "?"
    lines.extend(("", f"Diff base: {base_ref} ({base_commit})"))
    lines.extend(_format_changed_files(state))

    detected = _detected_category_labels(state.categories)
    if detected:
        lines.extend(("", "Detected change types: " + ", ".join(detected)))

    lines.extend(_format_target_summary(state))

    failures = [c for c in state.commands if int(c.get("exit_code", 0)) != 0]
    for command in failures:
        lines.extend(_format_command_failure(command))

    lines.extend((
        "",
        "Fix the failures above. The checks will re-run at the end of the next turn.",
    ))
    return "\n".join(lines)


def block_and_print(state: HookState) -> int:
    """Emit a blocking response and return a stop code.

    Parameters
    ----------
    state
        Hook execution state.

    Returns
    -------
    int
        Exit code for the hook.

    """
    payload = {"decision": "block", "reason": format_reason(state)}
    print(json.dumps(payload))
    return 0


def fail_state(state: HookState, message: str | None) -> int:
    """Mark the state as failed and emit a block response.

    Parameters
    ----------
    state
        Hook execution state.
    message
        Error message to include in the response.

    Returns
    -------
    int
        Exit code for the hook.

    """
    state.ok = False
    state.error = message
    return block_and_print(state)


def evaluate_changes(
    state: HookState, repo: Path, max_out: int, driver: BuildDriver
) -> int:
    """Select and execute checks based on detected changes.

    Parameters
    ----------
    state
        Hook execution state.
    repo
        Repository root path.
    max_out
        Maximum number of output characters to capture.
    driver
        Build driver selected for quality gates.

    Returns
    -------
    int
        Exit code for the hook.

    """
    cats = detect_categories(state.changed_files)
    state.categories = cats
    state.build_driver = driver.name

    requested = targets_for_categories(cats)
    state.targets_requested = requested
    if not requested:
        return 0

    available_targets, target_err = get_build_targets(repo, driver)
    if available_targets is None:
        fail_state(state, f"Could not enumerate build targets: {target_err}")
        return BLOCKED_STATUS

    run_targets = [t for t in requested if t in available_targets]
    skip_targets = [t for t in requested if t not in available_targets]
    state.targets_run = run_targets
    state.targets_skipped = skip_targets

    commands: list[CommandResult] = []
    code_targets = [
        t
        for t in targets_for_categories(cats, include=CODE_CATS)
        if t in available_targets
    ]
    md_targets = [
        t
        for t in targets_for_categories(cats, include=MD_CATS)
        if t in available_targets
    ]

    if code_targets:
        commands.append(
            run_build_targets(
                repo,
                BuildTargetRequest(driver, "code", code_targets),
                max_out,
            )
        )
    if md_targets:
        commands.append(
            run_build_targets(
                repo,
                BuildTargetRequest(driver, "markdown", md_targets),
                max_out,
            )
        )

    state.commands = commands

    if not commands:
        return 0

    ok_all = all(int(c.get("exit_code", 0)) == 0 for c in commands)
    if ok_all:
        return 0

    state.ok = False
    block_and_print(state)
    return BLOCKED_STATUS


def prepare_run_stop_checks(
    start_cwd: Path, base_ref: str, *, always_fetch: bool
) -> RunStopChecksPreparation:
    """Prepare repository state for ``run_stop_checks``.

    Parameters
    ----------
    start_cwd
        Working directory for git operations.
    base_ref
        Base git ref used for comparisons.
    always_fetch
        Whether to always fetch the base ref.

    Returns
    -------
    RunStopChecksPreparation
        Structured preparation result containing the populated hook state and
        repository root when preparation succeeded.

    """
    state = HookState(base_ref=base_ref)

    if not _is_executable_available("git"):
        return RunStopChecksPreparation(
            ok=False,
            exit_code=fail_state(state, "git not found on PATH"),
            state=state,
        )

    repo, err = repo_root(start_cwd)
    if repo is None:
        exit_code = (
            0
            if not start_cwd.exists()
            or (err and "not a git repository" in err.casefold())
            else fail_state(state, err)
        )
        return RunStopChecksPreparation(ok=False, exit_code=exit_code, state=state)

    ok, err, fetched = ensure_base_ref(repo, base_ref, always_fetch=always_fetch)
    state.fetched = fetched
    if not ok:
        return RunStopChecksPreparation(
            ok=False,
            exit_code=fail_state(state, err),
            state=state,
        )

    base_commit, err = merge_base(repo, base_ref)
    if base_commit is None:
        return RunStopChecksPreparation(
            ok=False,
            exit_code=fail_state(state, err),
            state=state,
        )
    state.base_commit = base_commit

    files, err = changed_files(repo, base_commit)
    if files is None:
        return RunStopChecksPreparation(
            ok=False,
            exit_code=fail_state(state, err),
            state=state,
        )

    state.changed_files = files
    return RunStopChecksPreparation(ok=True, exit_code=0, state=state, repo=repo)


def compush_check(repo: Path) -> int:
    """Block the stop with commit/push reminders when local work is not published.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    int
        Exit code for the hook (always 0 per hook contract).

    """
    upstream, _err = get_upstream_ref(repo)
    upstream_label = upstream or "origin (upstream not configured)"

    dirty, err = has_uncommitted_changes(repo)
    if err is not None:
        return 0
    if dirty:
        payload = {
            "decision": "block",
            "reason": f"Please commit and push to {upstream_label}",
        }
        print(json.dumps(payload))
        return 0

    if upstream is None:
        return 0

    ahead, err = has_unpushed_commits(repo, upstream)
    if err is not None or not ahead:
        return 0

    payload = {
        "decision": "block",
        "reason": f"Please push committed changes to {upstream_label}",
    }
    print(json.dumps(payload))
    return 0


def run_stop_checks(
    start_cwd: Path,
    base_ref: str,
    options: StopCheckOptions,
) -> int:
    """Run stop-hook checks for a given working directory.

    Parameters
    ----------
    start_cwd
        Working directory for git operations.
    base_ref
        Base git ref used for comparisons.
    options
        Runtime options for fetch, output capture, and commit/push reminders.

    Returns
    -------
    int
        Exit code for the hook.

    """
    preparation = prepare_run_stop_checks(
        start_cwd, base_ref, always_fetch=options.always_fetch
    )
    if not preparation.ok:
        return preparation.exit_code

    state = preparation.state
    repo = preparation.repo
    if repo is None:
        state.ok = False
        state.error = "internal error: repository preparation returned no repo"
        return block_and_print(state)

    if state.changed_files:
        cats = detect_categories(state.changed_files)
        state.categories = cats
        requested = targets_for_categories(cats)
        state.targets_requested = requested
        if requested:
            driver, err = select_build_driver(repo, options)
            if driver is None:
                return fail_state(state, err)

            rc = evaluate_changes(state, repo, options.max_out, driver)
            if rc != 0:
                return 0 if rc == BLOCKED_STATUS else rc

    if options.compush:
        return compush_check(repo)

    return 0
