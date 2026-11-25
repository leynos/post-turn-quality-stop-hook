"""Stop-hook orchestration pipeline.

Coordinates repository preparation, change detection, build-driver
selection, target execution, and the final pass/block decision. Branch-state
gates split into two stages: `refresh_upstream_head` collects the refreshed
upstream comparison through an `UpstreamHeadSource`, and
`unpushed_commits_gate` decides from that value. The caller supplies the
upstream port factory, so this module never imports the Git adapter. Entry
points: prepare_run_stop_checks and run_stop_checks.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.config import Config
    from post_turn_quality_stop_hook.upstream_port import UpstreamHeadSource

    #: Builds the upstream port for one repository state. The entry point
    #: supplies it so the pipeline never constructs the Git adapter itself.
    UpstreamSourceFactory = typ.Callable[[Path, "GitFacts | None"], UpstreamHeadSource]

from post_turn_quality_stop_hook.driver import (
    BuildDriver,
    _is_executable_available,
    get_build_targets,
    select_build_driver,
)
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.execution import run_build_targets
from post_turn_quality_stop_hook.formatting import block_and_print
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
from post_turn_quality_stop_hook.quality import (
    QualityStage,
    evaluate_changes,
    evaluation_report,
    select_categories,
)
from post_turn_quality_stop_hook.redaction import redact_remote_safely
from post_turn_quality_stop_hook.state import (
    HookState,
    PreparationBlock,
    PreparationReady,
    PreparationStop,
    StopCheckOptions,
)
from post_turn_quality_stop_hook.templates import render
from post_turn_quality_stop_hook.upstream_port import UpstreamComparison

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
        Hook error encountered while reading branch information.

    """

    is_protected: bool
    branch: str | None = None
    error: HookError | None = None


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
        Non-blocking hook error observed while evaluating the gate.

    """

    gate: str
    outcome: typ.Literal["pass", "skip", "block"]
    payload: dict[str, str] | None = None
    matched_branch: str | None = None
    match_kind: typ.Literal["local", "upstream"] | None = None
    error: HookError | None = None

    @property
    def should_block(self) -> bool:
        """Whether this decision should stop later gate evaluation."""
        return self.outcome == "block"


def _blocked_preparation(
    state: HookState, error: HookError, *, repo: Path | None = None
) -> PreparationBlock:
    """Return a blocking preparation outcome for an immutable snapshot."""
    return PreparationBlock(
        state=dataclasses.replace(state, ok=False, error=str(error)),
        repo=repo,
        error=error,
    )


def _missing_repository(error: HookError | None) -> bool:
    """Return whether Git reported that the directory is not a repository."""
    return error is not None and "not a git repository" in error.message.casefold()


def _prepare_repository(
    state: HookState, start_cwd: Path
) -> Path | PreparationStop | PreparationBlock:
    """Resolve the repository root or a quiet stop/block outcome."""
    repo, err = repo_root(start_cwd)
    if repo is not None:
        return repo
    if not start_cwd.exists() or _missing_repository(err):
        return PreparationStop(state=state)
    return _blocked_preparation(
        state,
        err or HookError("repo_root", "not a git repository"),
    )


def prepare_run_stop_checks(
    start_cwd: Path, base_ref: str, *, always_fetch: bool, config: Config
) -> PreparationReady | PreparationStop | PreparationBlock:
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
    PreparationReady | PreparationStop | PreparationBlock
        ``PreparationReady`` with the reporting snapshot and repository root,
        ``PreparationStop`` for a quiet non-repository exit, or
        ``PreparationBlock`` with the failure.

    """
    state = HookState(base_ref=base_ref)

    if not _is_executable_available("git"):
        return _blocked_preparation(
            state,
            HookError("prepare_run_stop_checks", "git not found on PATH"),
        )

    match _prepare_repository(state, start_cwd):
        case PreparationStop() | PreparationBlock() as outcome:
            return outcome
        case repo:
            pass

    facts = collect_git_facts(repo, config)
    state = dataclasses.replace(state, git_facts=facts)
    ok, err, fetched = ensure_base_ref(
        repo,
        base_ref,
        always_fetch=always_fetch,
        primary_remote=facts.primary_remote or "origin",
    )
    state = dataclasses.replace(state, fetched=fetched)
    if not ok:
        return _blocked_preparation(
            state,
            err or HookError("ensure_base_ref", "base ref unavailable"),
            repo=repo,
        )

    base_commit, err = merge_base(repo, base_ref)
    if base_commit is None:
        return _blocked_preparation(
            state,
            err or HookError("merge_base", "merge base unavailable"),
            repo=repo,
        )
    state = dataclasses.replace(state, base_commit=base_commit)

    files, err = changed_files(repo, base_commit)
    if files is None:
        return _blocked_preparation(
            state,
            err or HookError("changed_files", "changed files unavailable"),
            repo=repo,
        )
    state = dataclasses.replace(state, changed_files=tuple(files))

    return PreparationReady(state=state, repo=repo)


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
    repo: Path, options: StopCheckOptions
) -> BranchStateGateDecision:
    """Block when the working tree has uncommitted changes.

    Parameters
    ----------
    repo
        Repository path to check for uncommitted changes.
    options
        Runtime options whose config controls whether this gate runs.

    Returns
    -------
    BranchStateGateDecision
        Decision describing whether the gate passed, skipped, or blocked.

    """
    gate = "uncommitted_changes"
    if not options.config.gate_uncommitted_changes:
        return BranchStateGateDecision(gate=gate, outcome="skip")
    local_protection = _current_branch_protection(
        repo, options.config.protected_branches
    )
    if local_protection.error is not None:
        return BranchStateGateDecision(
            gate=gate,
            outcome="pass",
            error=local_protection.error,
        )
    if local_protection.is_protected:
        _log_branch_gate_outcome(gate, "skip", local_protection.branch, "local")
        return BranchStateGateDecision(
            gate=gate,
            outcome="skip",
            matched_branch=local_protection.branch,
            match_kind="local",
        )
    dirty, err = has_uncommitted_changes(repo)
    if err is not None:
        return BranchStateGateDecision(
            gate=gate,
            outcome="pass",
            error=err,
        )
    if not dirty:
        return BranchStateGateDecision(gate=gate, outcome="pass")
    payload = {
        "decision": "block",
        "reason": render("uncommitted_required.j2"),
    }
    _log_branch_gate_outcome(gate, "block", None, None)
    return BranchStateGateDecision(gate=gate, outcome="block", payload=payload)


def _upstream_label(comparison: UpstreamComparison) -> str:
    """Render the upstream target for the blocking message and warnings.

    A configured remote name reads naturally as ``origin/feature``. A URL or
    filesystem path does not, so it is shown as ``feature on <url>`` with its
    userinfo removed. A remote that cannot be rendered safely, and a local
    upstream branch, are shown by branch name alone so no part of a
    credential-bearing URL can reach a payload.
    """
    if comparison.remote == ".":
        return comparison.branch
    names = comparison.remote_names
    if names is not None and comparison.remote in names:
        return f"{comparison.remote}/{comparison.branch}"
    safe_remote = redact_remote_safely(comparison.remote)
    if safe_remote is None:
        return comparison.branch
    return f"{comparison.branch} on {safe_remote}"


def refresh_upstream_head(
    repo: Path,
    facts: GitFacts | None,
    options: StopCheckOptions,
    source: UpstreamHeadSource,
) -> BranchStateGateDecision | UpstreamComparison:
    """Collect the refreshed upstream state the unpushed gate decides from.

    This command step settles the preflight outcomes (disabled gate, missing
    facts, an unreadable upstream, or a protected branch) and then asks the
    upstream port for a refreshed comparison. The port performs the transport
    work and the HEAD comparison; this function logs a failed comparison and
    returns the value. Keeping this split lets ``unpushed_commits_gate`` stay a
    pure mapping from collected state to a decision.

    Parameters
    ----------
    repo
        Repository path.
    facts
        Collected Git facts, or ``None`` when collection failed.
    options
        Runtime options and configuration for the refresh.
    source
        Refreshed-upstream port supplied by the caller. The entry point builds
        the Git adapter in ``hook.main``; a test supplies a fake one.

    Returns
    -------
    BranchStateGateDecision | UpstreamComparison
        Terminal decision, or the refreshed upstream comparison.

    """
    gate = "unpushed_commits"
    if not options.config.gate_unpushed_commits:
        return BranchStateGateDecision(gate=gate, outcome="skip")

    preflight = _unpushed_preflight(repo, facts, options)
    if preflight is not None:
        return preflight

    comparison = source.refresh_comparison()
    if comparison is None:
        return BranchStateGateDecision(gate=gate, outcome="pass")
    if comparison.error is not None:
        logger.warning(
            "unpushed gate could not verify the upstream %s: %s",
            _upstream_label(comparison),
            comparison.error,
        )
    return comparison


def _emit_unpushed_block(comparison: UpstreamComparison) -> None:
    """Render, log, and print the unpushed block for a blocked comparison."""
    label = _upstream_label(comparison)
    payload = {
        "decision": "block",
        "reason": render("unpushed_required.j2", upstream_ref=label),
    }
    _log_branch_gate_outcome("unpushed_commits", "block", None, None)
    _emit_branch_gate_decision(
        BranchStateGateDecision(
            gate="unpushed_commits", outcome="block", payload=payload
        )
    )


def _unpushed_preflight(
    repo: Path, facts: GitFacts | None, options: StopCheckOptions
) -> BranchStateGateDecision | None:
    """Return a terminal decision, or ``None`` when the gate should proceed.

    Missing facts, an unreadable upstream, or a protected branch settles the
    gate here, before any transport work or comparison.
    """
    gate = "unpushed_commits"
    if facts is None:
        return BranchStateGateDecision(gate=gate, outcome="pass")
    if facts.upstream_error is not None:
        logger.warning(
            "unpushed gate could not resolve the upstream: %s",
            facts.upstream_error,
        )
        return BranchStateGateDecision(
            gate=gate,
            outcome="pass",
            error=facts.upstream_error,
        )
    if facts.upstream_remote is None or facts.upstream_branch is None:
        return BranchStateGateDecision(gate=gate, outcome="pass")
    return _protected_upstream_decision(repo, facts.upstream_branch, options, gate)


def _protected_upstream_decision(
    repo: Path, branch: str, options: StopCheckOptions, gate: str
) -> BranchStateGateDecision | None:
    """Skip the gate when either side of the comparison is protected."""
    local_decision = _protected_branch_skip_decision(
        gate,
        _current_branch_protection(repo, options.config.protected_branches),
        "local",
    )
    if local_decision is not None:
        _log_branch_gate_error(local_decision)
        return local_decision
    return _protected_branch_skip_decision(
        gate,
        BranchProtectionCheck(
            is_protected=branch in options.config.protected_branches,
            branch=branch,
        ),
        "upstream",
    )


def unpushed_commits_gate(
    refresh: BranchStateGateDecision | UpstreamComparison,
) -> BranchStateGateDecision:
    """Decide the unpushed gate from collected upstream state.

    The refresh step has already resolved the upstream, fetched its head, and
    compared it with HEAD. This decision function performs no I/O, no port
    call, no rendering, and no logging, so it can be exercised with a prepared
    comparison and its result inspected directly. ``_emit_unpushed_block``
    renders and logs the block when the hook reports it.

    Parameters
    ----------
    refresh
        Terminal preflight decision or collected upstream comparison.

    Returns
    -------
    BranchStateGateDecision
        Decision describing whether the gate passed, skipped, or blocked.

    """
    gate = "unpushed_commits"
    if isinstance(refresh, BranchStateGateDecision):
        return refresh
    if refresh.error is not None:
        return BranchStateGateDecision(
            gate=gate,
            outcome="pass",
            error=refresh.error,
        )
    if not refresh.ahead:
        return BranchStateGateDecision(gate=gate, outcome="pass")
    return BranchStateGateDecision(gate=gate, outcome="block")


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
        return BranchProtectionCheck(
            is_protected=False,
            error=err,
        )
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
        )
    if not protection.is_protected:
        return None
    _log_branch_gate_outcome(gate, "skip", protection.branch, match_kind)
    return BranchStateGateDecision(
        gate=gate,
        outcome="skip",
        matched_branch=protection.branch,
        match_kind=match_kind,
    )


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


def _log_branch_gate_error(decision: BranchStateGateDecision) -> None:
    """Log a non-blocking Git read failure while leaving the gate fail-open."""
    if decision.error is None:
        return
    logger.warning(
        "Branch-state gate %s failed at %s: %s",
        decision.gate,
        decision.error.operation,
        decision.error.message,
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
    context = _pr_rebase_context(repo, state, options)
    if context is None:
        return 0
    facts, summary = context

    match get_build_targets(repo, BuildDriver("make", options.make_bin, "Makefile")):
        case HookError() as available_targets:
            logger.warning("Could not enumerate build targets: %s", available_targets)
            makefile_has_typecheck_target = False
        case available_targets:
            makefile_has_typecheck_target = "typecheck" in available_targets
    payload = {
        "decision": "block",
        "reason": render(
            "rebase_required.j2",
            primary_remote=facts.primary_remote,
            base_branch=summary.base_branch,
            three_way_merge_is_configured=facts.three_way_merge_is_configured,
            makefile_has_typecheck_target=makefile_has_typecheck_target,
        ),
    }
    print(json.dumps(payload))
    return 0


def _pr_rebase_context(
    repo: Path, state: HookState, options: StopCheckOptions
) -> tuple[GitFacts, PullRequestSummary] | None:
    """Return the git facts and PR summary when the PR base is ahead.

    Return None when the gate is off, no remote or branch is known, or no PR matches.
    """
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
    """Return the git facts when the PR rebase gate applies, otherwise None.

    The gate applies when it is enabled and a primary remote is known.
    """
    if not options.config.gate_pr_rebase or facts is None:
        return None
    return facts if facts.primary_remote else None


def _pr_base_is_ahead(repo: Path, remote: str, summary: PullRequestSummary) -> bool:
    """Return whether the PR base has commits beyond the local merge base.

    The remote base branch is fetched first, and a failure reported by the Git
    helpers returns False.
    """
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
) -> HookState | None:
    """Run quality checks when changed files select build targets.

    Parameters
    ----------
    state
        Reporting snapshot whose changed files drive the checks.
    repo
        Repository path.
    options
        Runtime options used for driver selection and command output capture.

    Returns
    -------
    HookState | None
        A blocking report when checks fail or a driver cannot be selected, or
        ``None`` when the gate is disabled, no checks ran, or checks passed.
        Emission belongs to ``run_stop_checks``.

    """
    if not options.config.gate_quality_checks:
        return None

    selection = select_categories(state.changed_files)
    if not selection.targets_requested:
        return None

    match select_build_driver(repo, options):
        case HookError() as error:
            return dataclasses.replace(
                state,
                ok=False,
                categories=selection.categories,
                targets_requested=selection.targets_requested,
                error=str(error),
            )
        case None:
            _log_quality_gate_skip()
            return None
        case driver:
            stage = QualityStage(
                repo=repo,
                driver=driver,
                max_out=options.max_out,
                enumerate_targets=get_build_targets,
                run_commands=run_build_targets,
            )
            evaluation = evaluate_changes(stage, selection)
            if not evaluation.blocked:
                return None
            return evaluation_report(state, evaluation)


def run_branch_state_gates(
    repo: Path,
    state: HookState,
    options: StopCheckOptions,
    upstream_source: UpstreamHeadSource,
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
    upstream_source
        Refreshed-upstream port the unpushed gate decides from.

    Returns
    -------
    int
        Hook exit code. Returns ``0`` when a gate blocks or all gates pass.

    Notes
    -----
    Gate precedence is ``uncommitted_changes_gate``,
    ``unpushed_commits_gate``, then ``pr_rebase_check``. The unpushed gate is
    decided from state collected by ``refresh_upstream_head``. The first two
    gates stop the run when they block; otherwise evaluation continues to the
    next gate. ``pr_rebase_check`` returns the final hook exit code.

    """
    uncommitted_decision = uncommitted_changes_gate(repo, options)
    _log_branch_gate_error(uncommitted_decision)
    if uncommitted_decision.should_block:
        _emit_branch_gate_decision(uncommitted_decision)
        return 0
    refresh = refresh_upstream_head(repo, state.git_facts, options, upstream_source)
    unpushed_decision = unpushed_commits_gate(refresh)
    if unpushed_decision.should_block:
        if isinstance(refresh, UpstreamComparison):
            _emit_unpushed_block(refresh)
        else:
            _emit_branch_gate_decision(unpushed_decision)
        return 0
    return pr_rebase_check(repo, state, options)


def run_stop_checks(
    start_cwd: Path,
    base_ref: str,
    options: StopCheckOptions,
    upstream_source_for: UpstreamSourceFactory,
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
    upstream_source_for
        Builds the upstream port for the prepared repository and facts. The
        entry point supplies it, which keeps this module free of the Git
        adapter and lets a caller substitute another implementation.

    Returns
    -------
    int
        Exit code for the hook.

    """
    match prepare_run_stop_checks(
        start_cwd,
        base_ref,
        always_fetch=options.always_fetch,
        config=options.config,
    ):
        case PreparationStop():
            return 0
        case PreparationBlock() as blocked:
            return block_and_print(blocked.state)
        case PreparationReady(state=state, repo=repo):
            pass

    quality_report = run_quality_checks_if_needed(state, repo, options)
    if quality_report is not None:
        return block_and_print(quality_report)

    upstream_source = upstream_source_for(repo, state.git_facts)
    return run_branch_state_gates(repo, state, options, upstream_source)
