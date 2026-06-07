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
    current_branch,
    ensure_base_ref,
    ensure_remote_branch_ref,
    get_upstream_ref,
    has_uncommitted_changes,
    has_unpushed_commits,
    is_ancestor,
    merge_base,
    remote_url,
    repo_root,
)
from post_turn_quality_stop_hook.git_facts import GitFacts, collect_git_facts
from post_turn_quality_stop_hook.github import PullRequestSummary, lookup_pr
from post_turn_quality_stop_hook.state import (
    HookState,
    RunStopChecksPreparation,
    StopCheckOptions,
)
from post_turn_quality_stop_hook.templates import render


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


def uncommitted_changes_gate(repo: Path, options: StopCheckOptions) -> bool:
    """Block when the working tree has uncommitted changes.

    Parameters
    ----------
    repo
        Repository path to check for uncommitted changes.
    options
        Runtime options whose config controls whether this gate runs.

    Returns
    -------
    bool
        ``True`` when the gate is enabled and uncommitted changes are present,
        otherwise ``False``.

    """
    if not options.config.gate_uncommitted_changes:
        return False
    dirty, err = has_uncommitted_changes(repo)
    if err is not None or not dirty:
        return False
    payload = {
        "decision": "block",
        "reason": render("uncommitted_required.j2"),
    }
    print(json.dumps(payload))
    return True


def unpushed_commits_gate(
    repo: Path, state: HookState, options: StopCheckOptions
) -> bool:
    """Block when HEAD is ahead of its tracked upstream branch.

    Parameters
    ----------
    repo
        Repository path.
    state
        Hook state containing collected Git facts.
    options
        Runtime options whose config controls whether this gate runs.

    Returns
    -------
    bool
        ``True`` when the gate blocks because unpushed commits are present,
        otherwise ``False``.

    """
    if not options.config.gate_unpushed_commits:
        return False
    upstream = state.git_facts.upstream_ref if state.git_facts else None
    if upstream is None:
        return False
    ahead, err = has_unpushed_commits(repo, upstream)
    if err is not None or not ahead:
        return False
    payload = {
        "decision": "block",
        "reason": render("unpushed_required.j2", upstream_ref=upstream),
    }
    print(json.dumps(payload))
    return True


def pr_rebase_check(repo: Path, state: HookState, options: StopCheckOptions) -> int:
    """Block the stop when the open pull request base is ahead of this branch.

    Parameters
    ----------
    repo
        Repository path.
    state
        Hook state containing collected Git facts.
    options
        Runtime options and configuration used for PR lookup and rendering.

    Returns
    -------
    int
        Hook exit code, always ``0`` per the stop-hook contract.

    """
    context = _pr_rebase_context(repo, state, options)
    if context is None:
        return 0
    facts, summary = context

    available_targets, _target_err = get_build_targets(
        repo, BuildDriver("make", options.make_bin, "Makefile")
    )
    payload = {
        "decision": "block",
        "reason": render(
            "rebase_required.j2",
            primary_remote=facts.primary_remote,
            base_branch=summary.base_branch,
            three_way_merge_is_configured=facts.three_way_merge_is_configured,
            makefile_has_typecheck_target=(
                available_targets is not None and "typecheck" in available_targets
            ),
        ),
    }
    print(json.dumps(payload))
    return 0


def _pr_rebase_context(
    repo: Path, state: HookState, options: StopCheckOptions
) -> tuple[GitFacts, PullRequestSummary] | None:
    context: tuple[GitFacts, PullRequestSummary] | None = None
    facts = _pr_rebase_facts(options, state.git_facts)
    if facts is not None:
        remote = facts.primary_remote
        if remote is None:
            return context
        branch, _err = current_branch(repo)
        if branch is not None:
            url, _err = remote_url(repo, remote)
            if url is not None:
                summary = lookup_pr(
                    url, branch, timeout=options.config.github_timeout_seconds
                )
                if summary is not None and _pr_base_is_ahead(repo, remote, summary):
                    context = facts, summary
    return context


def _pr_rebase_facts(
    options: StopCheckOptions, facts: GitFacts | None
) -> GitFacts | None:
    if not options.config.gate_pr_rebase or facts is None:
        return None
    return facts if facts.primary_remote else None


def _pr_base_is_ahead(repo: Path, remote: str, summary: PullRequestSummary) -> bool:
    ok, _err, _fetched = ensure_remote_branch_ref(
        repo, remote, summary.base_branch, always_fetch=True
    )
    if not ok:
        return False

    local_base, _err = merge_base(repo, f"{remote}/{summary.base_branch}")
    if local_base is None or local_base == summary.base_oid:
        return False

    ancestor, _err = is_ancestor(repo, local_base, summary.base_oid)
    return ancestor is True


def run_quality_checks_if_needed(
    state: HookState, repo: Path, options: StopCheckOptions
) -> int | None:
    """Run quality checks when changed files select build targets.

    Parameters
    ----------
    state
        Hook state to update with categories and requested targets.
    repo
        Repository path.
    options
        Runtime options used for driver selection and command output capture.

    Returns
    -------
    int | None
        ``None`` when no checks ran or checks passed. Returns ``0`` when
        ``evaluate_changes`` blocked under the hook contract, and any other
        integer error code returned by ``evaluate_changes``.

    Notes
    -----
    ``run_quality_checks_if_needed`` sets ``state.categories`` and
    ``state.targets_requested`` before selecting the build driver.

    """
    if not state.changed_files:
        return None

    cats = detect_categories(state.changed_files)
    state.categories = cats
    requested = targets_for_categories(cats)
    state.targets_requested = requested
    if not requested:
        return None

    driver, err = select_build_driver(repo, options)
    if driver is None:
        return fail_state(state, err)

    rc = evaluate_changes(state, repo, options.max_out, driver)
    if rc == 0:
        return None
    return 0 if rc == BLOCKED_STATUS else rc


def run_branch_state_gates(
    repo: Path, state: HookState, options: StopCheckOptions
) -> int:
    """Run branch-state gates in precedence order.

    Parameters
    ----------
    repo
        Repository path.
    state
        Hook state containing collected Git facts.
    options
        Runtime options and configuration for gate evaluation.

    Returns
    -------
    int
        Hook exit code. Returns ``0`` when a gate blocks or all gates pass.

    Notes
    -----
    Gate precedence is ``uncommitted_changes_gate``,
    ``unpushed_commits_gate``, then ``pr_rebase_check``. The first two gates
    return ``True`` when they block; otherwise evaluation continues to the next
    gate. ``pr_rebase_check`` returns the final hook exit code.

    """
    if uncommitted_changes_gate(repo, options):
        return 0
    if unpushed_commits_gate(repo, state, options):
        return 0
    return pr_rebase_check(repo, state, options)


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

    quality_result = run_quality_checks_if_needed(state, repo, options)
    if quality_result is not None:
        return quality_result

    return run_branch_state_gates(repo, state, options)
