"""Stop-hook orchestration pipeline.

Coordinates repository preparation, change detection,
build-driver selection, target execution, and the final
pass/block decision. Entry points: prepare_run_stop_checks
and run_stop_checks.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from post_turn_quality_stop_hook.decision_log import DecisionLog

from post_turn_quality_stop_hook.driver import (
    BuildDriver,
    _is_executable_available,
    get_build_targets,
    select_build_driver,
    selection_reason,
)
from post_turn_quality_stop_hook.execution import (
    BLOCKED_STATUS,
    CODE_CATS,
    MD_CATS,
    BuildTargetRequest,
    CommandResult,
    build_command,
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
    head_commit,
    is_ancestor,
    merge_base,
    remote_names,
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

logger = logging.getLogger(__name__)


@dataclasses.dataclass(slots=True, frozen=True)
class BranchProtectionCheck:
    """Result of comparing a branch name with configured protected branches.

    Attributes
    ----------
    is_protected
        Whether the branch matched the protected branch set.
    branch
        Branch name compared with ``protected_branches`` when available.
    error
        Error encountered while reading branch information.

    """

    is_protected: bool
    branch: str | None = None
    error: str | None = None


@dataclasses.dataclass(slots=True, frozen=True)
class PrRebaseEvaluation:
    """Result of evaluating whether the pull request base is ahead.

    Attributes
    ----------
    context
        Facts and pull-request summary when the base is ahead.
    outcome
        ``pass`` when the base is known not to be ahead, ``skip`` when the
        check could not determine the result.
    reason
        Short explanation recorded in the decision log.

    """

    context: tuple[GitFacts, PullRequestSummary] | None
    outcome: typ.Literal["pass", "skip"]
    reason: str


@dataclasses.dataclass(slots=True, frozen=True)
class BranchStateGateDecision:
    """Decision returned by a branch-state gate.

    Attributes
    ----------
    gate
        Stable gate name for logs and tests.
    outcome
        One of ``pass``, ``skip``, or ``block``.
    payload
        JSON payload to emit when the gate blocks.
    matched_branch
        Protected branch that caused a skip, when applicable.
    match_kind
        Whether the protected match came from the local or upstream branch.
    error
        Non-blocking Git read error observed while evaluating the gate.
    reason
        Short explanation recorded in the decision log.

    """

    gate: str
    outcome: typ.Literal["pass", "skip", "block"]
    payload: dict[str, str] | None = None
    matched_branch: str | None = None
    match_kind: typ.Literal["local", "upstream"] | None = None
    error: str | None = None
    reason: str | None = None

    @property
    def should_block(self) -> bool:
        """Whether this decision should stop later gate evaluation."""
        return self.outcome == "block"


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
    log = state.decision_log

    requested = targets_for_categories(cats)
    state.targets_requested = requested
    if not requested:
        _record_quality(log, state)
        return 0

    available_targets, target_err = _enumerate_targets(log, repo, driver)
    if available_targets is None:
        logger.warning("Could not enumerate build targets: %s", target_err)
        # The error text comes from the build tool's output and the log must
        # never carry command output, so both the gate reason and the logged
        # block reason stay fixed strings; the response keeps the detail.
        _record_gate(
            log, "quality_checks", "block", "could not enumerate build targets"
        )
        fail_state(
            state,
            f"Could not enumerate build targets: {target_err}",
            log_reason="could not enumerate build targets",
        )
        return BLOCKED_STATUS

    _apply_target_selection(state, cats, available_targets)
    _record_quality(log, state)

    commands = [
        _run_build_group(state, repo, request, max_out)
        for request in _build_group_requests(cats, available_targets, driver)
    ]
    state.commands = commands

    if not commands:
        _record_gate(log, "quality_checks", "pass", "no build targets present")
        return 0

    ok_all = all(int(c.get("exit_code", 0)) == 0 for c in commands)
    if ok_all:
        _record_gate(log, "quality_checks", "pass", "all quality targets passed")
        return 0

    _record_gate(log, "quality_checks", "block", _quality_block_reason(commands))
    state.ok = False
    block_and_print(state)
    return BLOCKED_STATUS


def _apply_target_selection(
    state: HookState, cats: set[str], available_targets: set[str]
) -> None:
    """Store the run and skipped targets selected for the changed categories."""
    run_targets = select_targets(cats, available_targets)
    state.targets_available = sorted(available_targets)
    state.targets_run = run_targets
    state.targets_present = run_targets
    state.targets_skipped = [
        target for target in state.targets_requested if target not in available_targets
    ]


def _build_group_requests(
    cats: set[str], available_targets: set[str], driver: BuildDriver
) -> list[BuildTargetRequest]:
    """Build one request per non-empty target group, in execution order."""
    groups = (("code", CODE_CATS), ("markdown", MD_CATS))
    requests: list[BuildTargetRequest] = []
    for kind, include in groups:
        targets = [
            target
            for target in targets_for_categories(cats, include=include)
            if target in available_targets
        ]
        if targets:
            requests.append(BuildTargetRequest(driver, kind, targets))
    return requests


def _run_build_group(
    state: HookState,
    repo: Path,
    request: BuildTargetRequest,
    max_out: int,
) -> CommandResult:
    """Run one build-target group, recording its lifecycle in the log."""
    log = state.decision_log
    progress = None
    if log is not None:
        # The executable can come from ``POST_TURN_MAKE_BIN`` or
        # ``POST_TURN_NETSUKE_BIN``, so log the driver name in its place and
        # keep the environment-derived path out of the durable record.
        command = " ".join([
            request.driver.name,
            *build_command(request.driver, request.targets)[1:],
        ])
        progress = log.command_start(request.kind, command, request.targets)
    result = run_build_targets(repo, request, max_out)
    if log is not None and progress is not None:
        log.command_finish(progress, int(result.get("exit_code", 0)))
    return result


def _record_quality(log: DecisionLog | None, state: HookState) -> None:
    """Record quality-gate selection facts in the decision log."""
    if log is None:
        return
    log.set_quality(
        build_driver=state.build_driver,
        build_driver_reason=state.build_driver_reason,
        targets_requested=list(state.targets_requested),
        targets_available=list(state.targets_available),
        targets_run=list(state.targets_run),
        targets_skipped=list(state.targets_skipped),
    )


def _quality_block_reason(commands: list[CommandResult]) -> str:
    """Summarise failed command exit codes without their output."""
    failures = [
        f"{c.get('kind', 'command')} exited {c.get('exit_code', '?')}"
        for c in commands
        if int(c.get("exit_code", 0)) != 0
    ]
    return "quality checks failed: " + ", ".join(failures)


def _record_gate(
    log: DecisionLog | None, gate: str, outcome: str, reason: str | None = None
) -> None:
    """Record a gate outcome when a decision log is present."""
    if log is not None:
        log.record_gate(gate, outcome, reason)


def _log_exclude(log: DecisionLog | None) -> tuple[Path, ...]:
    """Return directories the log itself occupies inside the worktree."""
    return (log.directory,) if log is not None else ()


def _enumerate_targets(
    log: DecisionLog | None, repo: Path, driver: BuildDriver
) -> tuple[set[str] | None, str | None]:
    """Enumerate available targets, recording blocking tool invocations."""
    progress = None
    if log is not None and driver.name == "netsuke":
        # ``netsuke generate`` is a blocking subprocess. The executable can
        # come from ``POST_TURN_NETSUKE_BIN``, so record the driver name in
        # its place and keep the environment-derived path out of the log.
        progress = log.command_start("targets", f"{driver.name} generate", [])
    available, err = get_build_targets(repo, driver)
    if log is not None and progress is not None:
        log.command_finish(progress, 0 if available is not None else 1)
    return available, err


def prepare_run_stop_checks(
    start_cwd: Path,
    base_ref: str,
    options: StopCheckOptions,
    *,
    decision_log: DecisionLog | None = None,
) -> RunStopChecksPreparation:
    """Prepare repository state for ``run_stop_checks``.

    Parameters
    ----------
    start_cwd
        Working directory for git operations.
    base_ref
        Base git ref used for comparisons.
    options
        Runtime options supplying the fetch policy and configuration.
    decision_log
        Optional durable log for repository facts.

    Returns
    -------
    RunStopChecksPreparation
        Structured preparation result containing the populated hook state and
        repository root when preparation succeeded.

    """
    state = HookState(base_ref=base_ref, decision_log=decision_log)

    if not _is_executable_available("git"):
        return RunStopChecksPreparation(
            ok=False,
            exit_code=fail_state(
                state,
                "git not found on PATH",
                log_reason="git not found on PATH",
            ),
            state=state,
        )

    repo, err = repo_root(start_cwd)
    if repo is None:
        exit_code = (
            0
            if not start_cwd.exists()
            or (err and "not a git repository" in err.casefold())
            else fail_state(state, err, log_reason="could not resolve repository root")
        )
        return RunStopChecksPreparation(ok=False, exit_code=exit_code, state=state)

    state.git_facts = collect_git_facts(repo, options.config)
    _record_repository_facts(decision_log, repo, base_ref, state.git_facts)
    fetch_progress = None
    if decision_log is not None:
        # ``ensure_base_ref`` may run ``git fetch``, which can block on the
        # network. Record the step before starting it so a timeout is visible.
        fetch_progress = decision_log.command_start("fetch", "git fetch", [])
    ok, err, fetched = ensure_base_ref(
        repo,
        base_ref,
        always_fetch=options.always_fetch,
        primary_remote=state.git_facts.primary_remote or "origin",
    )
    if decision_log is not None and fetch_progress is not None:
        decision_log.command_finish(
            fetch_progress, 0 if ok else 1, {"fetched": fetched}
        )
    state.fetched = fetched
    if not ok:
        return RunStopChecksPreparation(
            ok=False,
            exit_code=fail_state(state, err, log_reason="could not ensure base ref"),
            state=state,
        )

    base_commit, err = merge_base(repo, base_ref)
    if base_commit is None:
        return RunStopChecksPreparation(
            ok=False,
            exit_code=fail_state(state, err, log_reason="could not find merge base"),
            state=state,
        )
    state.base_commit = base_commit

    files, err = changed_files(repo, base_commit, exclude=_log_exclude(decision_log))
    if files is None:
        return RunStopChecksPreparation(
            ok=False,
            exit_code=fail_state(state, err, log_reason="could not list changed files"),
            state=state,
        )

    state.changed_files = files
    return RunStopChecksPreparation(ok=True, exit_code=0, state=state, repo=repo)


def _record_repository_facts(
    log: DecisionLog | None,
    repo: Path,
    base_ref: str,
    facts: GitFacts | None,
) -> None:
    """Record best-effort repository facts for the decision log."""
    if log is None:
        return
    # These facts exist only for the log, so no failure to collect them may
    # abort preparation. ``git.run`` normally reports failures in its result,
    # but starting the process can still raise ``OSError``.
    try:
        branch, _branch_err = current_branch(repo)
        head, _head_err = head_commit(repo)
        dirty, _dirty_err = has_uncommitted_changes(repo, exclude=_log_exclude(log))
        unpushed: bool | None = None
        if facts is not None and facts.upstream_ref is not None:
            unpushed, _unpushed_err = has_unpushed_commits(repo, facts.upstream_ref)
    except OSError:
        return
    # Query these again rather than reuse an earlier stage's facts: the
    # quality checks run between preparation and the branch-state gates, so
    # each caller needs the repository state at its own point in time.
    log.set_repository(
        repo_root=str(repo),
        branch=branch,
        head=head,
        base_ref=base_ref,
        dirty=dirty,
        unpushed=unpushed,
    )


def compush_check(repo: Path) -> int:
    """Retain the historical commit/push reminder for downstream imports.

    This compatibility helper is not part of ``run_stop_checks``. The active
    hook uses its separate configuration-driven branch-state gates.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    int
        Exit code for the hook (always zero per the legacy contract).

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


def uncommitted_changes_gate(
    repo: Path,
    options: StopCheckOptions,
    *,
    exclude: cabc.Iterable[Path] = (),
) -> BranchStateGateDecision:
    """Block when the working tree has uncommitted changes.

    Parameters
    ----------
    repo
        Repository path to check for uncommitted changes.
    options
        Runtime options whose config controls whether this gate runs.
    exclude
        Directories whose untracked contents must be ignored, such as a
        decision-log directory inside the worktree.

    Returns
    -------
    BranchStateGateDecision
        Decision describing whether the gate passed, skipped, or blocked.

    """
    gate = "uncommitted_changes"
    if not options.config.gate_uncommitted_changes:
        return BranchStateGateDecision(
            gate=gate, outcome="skip", reason="gate disabled by configuration"
        )
    local_protection = _current_branch_protection(
        repo, options.config.protected_branches
    )
    if local_protection.error is not None:
        return BranchStateGateDecision(
            gate=gate,
            outcome="pass",
            error=local_protection.error,
            reason="git error",
        )
    if local_protection.is_protected:
        _log_branch_gate_outcome(gate, "skip", local_protection.branch, "local")
        return BranchStateGateDecision(
            gate=gate,
            outcome="skip",
            matched_branch=local_protection.branch,
            match_kind="local",
            reason=f"protected branch {local_protection.branch} (local)",
        )
    dirty, err = has_uncommitted_changes(repo, exclude=exclude)
    if err is not None:
        return BranchStateGateDecision(
            gate=gate, outcome="pass", error=err, reason="git error"
        )
    if not dirty:
        return BranchStateGateDecision(
            gate=gate, outcome="pass", reason="working tree clean"
        )
    payload = {
        "decision": "block",
        "reason": render("uncommitted_required.j2"),
    }
    _log_branch_gate_outcome(gate, "block", None, None)
    return BranchStateGateDecision(
        gate=gate,
        outcome="block",
        payload=payload,
        reason="uncommitted changes present",
    )


def unpushed_commits_gate(
    repo: Path, state: HookState, options: StopCheckOptions
) -> BranchStateGateDecision:
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
    BranchStateGateDecision
        Decision describing whether the gate passed, skipped, or blocked.

    """
    gate = "unpushed_commits"
    if not options.config.gate_unpushed_commits:
        return BranchStateGateDecision(
            gate=gate, outcome="skip", reason="gate disabled by configuration"
        )
    if state.git_facts is None or state.git_facts.upstream_ref is None:
        return BranchStateGateDecision(
            gate=gate, outcome="pass", reason="no upstream configured"
        )
    upstream = state.git_facts.upstream_ref
    local_protection = _current_branch_protection(
        repo, options.config.protected_branches
    )
    local_decision = _protected_branch_skip_decision(gate, local_protection, "local")
    if local_decision is not None:
        return local_decision
    upstream_protection = _tracked_branch_protection(
        repo,
        upstream,
        state.git_facts.primary_remote,
        options.config.protected_branches,
    )
    upstream_decision = _protected_branch_skip_decision(
        gate, upstream_protection, "upstream"
    )
    if upstream_decision is not None:
        return upstream_decision
    ahead, err = has_unpushed_commits(repo, upstream)
    if err is not None or not ahead:
        reason = "git error" if err is not None else "no unpushed commits"
        return BranchStateGateDecision(
            gate=gate, outcome="pass", error=err, reason=reason
        )
    payload = {
        "decision": "block",
        "reason": render("unpushed_required.j2", upstream_ref=upstream),
    }
    _log_branch_gate_outcome(gate, "block", None, None)
    return BranchStateGateDecision(
        gate=gate,
        outcome="block",
        payload=payload,
        reason=f"unpushed commits ahead of {upstream}",
    )


def _emit_branch_gate_decision(decision: BranchStateGateDecision) -> None:
    """Emit a blocking gate payload when the decision carries one."""
    if decision.payload is not None:
        print(json.dumps(decision.payload))


def _current_branch_protection(
    repo: Path, protected_branches: tuple[str, ...]
) -> BranchProtectionCheck:
    """Return protected-branch status for the current local branch."""
    branch, err = current_branch(repo)
    if err is not None:
        return BranchProtectionCheck(is_protected=False, error=err)
    if branch is None:
        return BranchProtectionCheck(is_protected=False)
    return BranchProtectionCheck(
        is_protected=branch in protected_branches,
        branch=branch,
    )


def _protected_branch_skip_decision(
    gate: str,
    protection: BranchProtectionCheck,
    match_kind: typ.Literal["local", "upstream"],
) -> BranchStateGateDecision | None:
    """Return a skip/pass decision when branch protection decides the gate."""
    if protection.error is not None:
        return BranchStateGateDecision(
            gate=gate,
            outcome="pass",
            error=protection.error,
            reason="git error",
        )
    if not protection.is_protected:
        return None
    _log_branch_gate_outcome(gate, "skip", protection.branch, match_kind)
    return BranchStateGateDecision(
        gate=gate,
        outcome="skip",
        matched_branch=protection.branch,
        match_kind=match_kind,
        reason=f"protected branch {protection.branch} ({match_kind})",
    )


def _tracked_branch_protection(
    repo: Path,
    upstream_ref: str,
    primary_remote: str | None,
    protected_branches: tuple[str, ...],
) -> BranchProtectionCheck:
    """Return protected-branch status for a tracked upstream ref."""
    remotes, err = remote_names(repo)
    if err is not None:
        return BranchProtectionCheck(is_protected=False, error=err)
    branch = _tracked_branch_name(upstream_ref, primary_remote, remotes or [])
    return BranchProtectionCheck(
        is_protected=branch in protected_branches,
        branch=branch,
    )


def _tracked_branch_name(
    upstream_ref: str,
    primary_remote: str | None,
    configured_remotes: cabc.Iterable[str],
) -> str:
    """Strip the tracked remote prefix from an upstream ref."""
    for remote in _candidate_remote_prefixes(primary_remote, configured_remotes):
        remote_prefix = f"{remote}/"
        if upstream_ref.startswith(remote_prefix):
            return upstream_ref.removeprefix(remote_prefix)
    return upstream_ref.split("/", maxsplit=1)[-1]


def _candidate_remote_prefixes(
    primary_remote: str | None, configured_remotes: cabc.Iterable[str]
) -> tuple[str, ...]:
    """Return candidate remote names, longest first, for upstream parsing."""
    candidates: set[str] = {remote for remote in configured_remotes if remote != ""}
    if primary_remote is not None and primary_remote != "":
        candidates.add(primary_remote)
    return tuple(
        sorted(
            candidates,
            key=_string_length,
            reverse=True,
        )
    )


def _string_length(value: str) -> int:
    """Return string length for typed sorting callbacks."""
    return len(value)


def _log_branch_gate_outcome(
    gate: str,
    outcome: typ.Literal["skip", "block"],
    matched_branch: str | None,
    match_kind: typ.Literal["local", "upstream"] | None,
) -> None:
    """Log bounded branch-state gate telemetry without changing hook stdout."""
    logger.info(
        "branch_state_gate_%s",
        outcome,
        extra={
            "gate": gate,
            "outcome": outcome,
            "matched_branch": matched_branch,
            "match_kind": match_kind,
        },
    )


def _log_quality_gate_skip() -> None:
    """Log bounded quality-gate skip telemetry without changing hook stdout."""
    logger.info(
        "quality_gate_skip",
        extra={
            "operation": "quality_gate_skip",
            "build_driver": "auto",
            "manifests_missing": True,
        },
    )


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
    log = state.decision_log
    if not options.config.gate_pr_rebase:
        _record_gate(log, "pr_rebase", "skip", "gate disabled by configuration")
        return 0
    if state.git_facts is None or state.git_facts.primary_remote is None:
        _record_gate(log, "pr_rebase", "skip", "no primary remote")
        return 0
    evaluation = _pr_rebase_context(repo, state, options)
    if evaluation.context is None:
        _record_gate(log, "pr_rebase", evaluation.outcome, evaluation.reason)
        return 0
    facts, summary = evaluation.context

    available_targets, target_err = _enumerate_targets(
        log, repo, BuildDriver("make", options.make_bin, "Makefile")
    )
    if available_targets is None:
        logger.warning("Could not enumerate build targets: %s", target_err)
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
    _record_gate(log, "pr_rebase", "block", "pull request base is ahead")
    if log is not None:
        log.mark_block("pull request base is ahead")
    print(json.dumps(payload))
    return 0


def _pr_rebase_context(
    repo: Path, state: HookState, options: StopCheckOptions
) -> PrRebaseEvaluation:
    facts = _pr_rebase_facts(options, state.git_facts)
    if facts is None or facts.primary_remote is None:
        return PrRebaseEvaluation(None, "skip", "no primary remote")
    summary = _locate_pull_request(repo, facts.primary_remote, options)
    if isinstance(summary, PrRebaseEvaluation):
        return summary
    ahead, reason = _compare_pr_base(repo, state, facts.primary_remote, summary)
    if ahead is None:
        return PrRebaseEvaluation(None, "skip", reason)
    if not ahead:
        return PrRebaseEvaluation(None, "pass", reason)
    return PrRebaseEvaluation((facts, summary), "pass", reason)


def _locate_pull_request(
    repo: Path, remote: str, options: StopCheckOptions
) -> PullRequestSummary | PrRebaseEvaluation:
    """Find the open pull request, or return the skip evaluation instead."""
    branch, _err = current_branch(repo)
    if branch is None:
        return PrRebaseEvaluation(
            None, "skip", "could not determine the current branch"
        )
    url, _err = remote_url(repo, remote)
    if url is None:
        return PrRebaseEvaluation(None, "skip", "could not determine the remote URL")
    summary = lookup_pr(url, branch, timeout=options.config.github_timeout_seconds)
    if summary is None:
        return PrRebaseEvaluation(
            None, "skip", "could not determine an open pull request"
        )
    return summary


def _compare_pr_base(
    repo: Path, state: HookState, remote: str, summary: PullRequestSummary
) -> tuple[bool | None, str]:
    """Compare the PR base with the local merge-base, recording the fetch."""
    log = state.decision_log
    progress = None
    if log is not None:
        # The PR-base check always fetches its branch, which can block on the
        # network. Record the step before starting it so a timeout is visible.
        progress = log.command_start("fetch", "git fetch", [])
    ahead, reason = _pr_base_is_ahead(repo, remote, summary)
    if log is not None and progress is not None:
        log.command_finish(progress, 0 if ahead is not None else 1)
    return ahead, reason


def _pr_rebase_facts(
    options: StopCheckOptions, facts: GitFacts | None
) -> GitFacts | None:
    if not options.config.gate_pr_rebase or facts is None:
        return None
    return facts if facts.primary_remote else None


def _pr_base_is_ahead(
    repo: Path, remote: str, summary: PullRequestSummary
) -> tuple[bool | None, str]:
    """Return whether the PR base is ahead, or ``None`` when undetermined."""
    ok, _err, _fetched = ensure_remote_branch_ref(
        repo, remote, summary.base_branch, always_fetch=True
    )
    if not ok:
        return None, "could not fetch the pull request base"

    local_base, _err = merge_base(repo, f"{remote}/{summary.base_branch}")
    if local_base is None:
        return None, "could not compute the pull request merge base"
    if local_base == summary.base_oid:
        return False, "pull request base is not ahead"

    ancestor, _err = is_ancestor(repo, local_base, summary.base_oid)
    if ancestor is None:
        return None, "could not compare the pull request base"
    if ancestor:
        return True, "pull request base is ahead"
    return False, "pull request base is not ahead"


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
    log = state.decision_log
    if not state.changed_files:
        _record_gate(log, "quality_checks", "skip", "no changed files")
        return None

    cats = detect_categories(state.changed_files)
    state.categories = cats
    requested = targets_for_categories(cats)
    state.targets_requested = requested
    if not requested:
        _record_gate(
            log, "quality_checks", "skip", "no quality targets for changed files"
        )
        return None

    driver, err = select_build_driver(repo, options)
    if driver is None:
        # Record the targets the changed files requested even though no driver
        # can run them; otherwise the log cannot explain the skip.
        _record_quality(log, state)
        if err is None:
            _log_quality_gate_skip()
            _record_gate(
                log, "quality_checks", "skip", "no supported build driver available"
            )
            return None
        # The error names the requested driver, which comes from the
        # environment, so the log gets a fixed reason instead.
        _record_gate(log, "quality_checks", "block", "could not select build driver")
        return fail_state(state, err, log_reason="could not select build driver")

    state.build_driver_reason = selection_reason(options, driver)
    _record_quality(log, state)
    rc = evaluate_changes(state, repo, options.max_out, driver)
    if rc not in {0, BLOCKED_STATUS}:
        _record_gate(log, "quality_checks", "error", f"quality checks returned {rc}")
        return rc
    return None if rc == 0 else 0


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
    log = state.decision_log
    with _stage(log, "gate_uncommitted_changes"):
        uncommitted_decision = uncommitted_changes_gate(
            repo, options, exclude=_log_exclude(log)
        )
    _record_gate_decision(state, uncommitted_decision)
    if uncommitted_decision.should_block:
        _emit_branch_gate_decision(uncommitted_decision)
        return 0
    with _stage(log, "gate_unpushed_commits"):
        unpushed_decision = unpushed_commits_gate(repo, state, options)
    _record_gate_decision(state, unpushed_decision)
    if unpushed_decision.should_block:
        _emit_branch_gate_decision(unpushed_decision)
        return 0
    with _stage(log, "gate_pr_rebase"):
        return pr_rebase_check(repo, state, options)


def _record_gate_decision(state: HookState, decision: BranchStateGateDecision) -> None:
    """Record a branch-state gate decision in the decision log."""
    log = state.decision_log
    if log is None:
        return
    log.record_gate(decision.gate, decision.outcome, decision.reason)
    if decision.outcome == "block":
        log.mark_block(decision.reason or decision.gate)


def run_stop_checks(
    start_cwd: Path,
    base_ref: str,
    options: StopCheckOptions,
    *,
    decision_log: DecisionLog | None = None,
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
    decision_log
        Optional durable decision log for this invocation.

    Returns
    -------
    int
        Exit code for the hook.

    """
    with _stage(decision_log, "prepare"):
        preparation = prepare_run_stop_checks(
            start_cwd,
            base_ref,
            options,
            decision_log=decision_log,
        )
    if not preparation.ok:
        return preparation.exit_code

    state = preparation.state
    repo = preparation.repo
    if repo is None:
        state.ok = False
        state.error = "internal error: repository preparation returned no repo"
        state.log_reason = "internal error: repository preparation returned no repo"
        return block_and_print(state)

    with _stage(decision_log, "quality_checks"):
        quality_result = run_quality_checks_if_needed(state, repo, options)
    if quality_result is not None:
        return quality_result

    with _stage(decision_log, "branch_state_gates"):
        return run_branch_state_gates(repo, state, options)


def _stage(
    decision_log: DecisionLog | None, name: str
) -> contextlib.AbstractContextManager[None]:
    """Return a stage recorder or a no-op context manager."""
    if decision_log is None:
        return contextlib.nullcontext()
    return decision_log.stage(name)
