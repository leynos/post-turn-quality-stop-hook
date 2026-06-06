"""Stop-hook orchestration pipeline.

Coordinates repository preparation, change detection,
build-driver selection, target execution, and the final
pass/block decision. Entry points: prepare_run_stop_checks
and run_stop_checks.
"""

from __future__ import annotations

import json
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.config import Config

from post_turn_quality_stop_hook.driver import (
    BuildDriver,
    _is_executable_available,
    get_build_targets,
    select_build_driver,
)
from post_turn_quality_stop_hook.execution import (
    BLOCKED_STATUS,
    CODE_CATS,
    MD_CATS,
    BuildTargetRequest,
    CommandResult,
    run_build_targets,
    select_targets,
    targets_for_categories,
)
from post_turn_quality_stop_hook.formatting import (
    block_and_print,
    detect_categories,
    fail_state,
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
from post_turn_quality_stop_hook.git_facts import collect_git_facts
from post_turn_quality_stop_hook.state import (
    HookState,
    RunStopChecksPreparation,
    StopCheckOptions,
)


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

    run_targets = select_targets(cats, available_targets)
    skip_targets = [t for t in requested if t not in available_targets]
    state.targets_run = run_targets
    state.targets_present = run_targets
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
    start_cwd: Path, base_ref: str, *, always_fetch: bool, config: Config
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
    config
        Merged hook configuration used for primary remote resolution.

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

    state.git_facts = collect_git_facts(repo, config)
    ok, err, fetched = ensure_base_ref(
        repo,
        base_ref,
        always_fetch=always_fetch,
        primary_remote=state.git_facts.primary_remote or "origin",
    )
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
        start_cwd,
        base_ref,
        always_fetch=options.always_fetch,
        config=options.config,
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
