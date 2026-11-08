"""Stop-hook orchestration pipeline.

Coordinates repository preparation, change detection,
build-driver selection, target execution, and the final
pass/block decision. Entry points: prepare_run_stop_checks
and run_stop_checks.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from post_turn_quality_stop_hook.config import Config

from post_turn_quality_stop_hook.driver import (
    BuildDriver,
    _is_executable_available,
    get_build_targets,
    select_build_driver,
)
from post_turn_quality_stop_hook.errors import HookError
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
    remote_names,
    remote_url,
    repo_root,
)
from post_turn_quality_stop_hook.git_facts import GitFacts, collect_git_facts
from post_turn_quality_stop_hook.github import PullRequestSummary, lookup_pr
from post_turn_quality_stop_hook.quality import (
    evaluate_changes,
    evaluation_report,
    select_categories,
)
from post_turn_quality_stop_hook.state import (
    HookState,
    PreparationBlock,
    PreparationReady,
    PreparationStop,
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


def _missing_repository(error: str | None) -> bool:
    """Return whether Git reported that the directory is not a repository."""
    return error is not None and "not a git repository" in error.casefold()


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
        HookError("repo_root", err or "not a git repository"),
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
            HookError("ensure_base_ref", err or "base ref unavailable"),
            repo=repo,
        )

    base_commit, err = merge_base(repo, base_ref)
    if base_commit is None:
        return _blocked_preparation(
            state,
            HookError("merge_base", err or "merge base unavailable"),
            repo=repo,
        )
    state = dataclasses.replace(state, base_commit=base_commit)

    files, err = changed_files(repo, base_commit)
    if files is None:
        return _blocked_preparation(
            state,
            HookError("changed_files", err or "changed files unavailable"),
            repo=repo,
        )
    state = dataclasses.replace(state, changed_files=files)

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
            error=HookError("current_branch", local_protection.error),
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
            error=HookError("has_uncommitted_changes", err),
        )
    if not dirty:
        return BranchStateGateDecision(gate=gate, outcome="pass")
    payload = {
        "decision": "block",
        "reason": render("uncommitted_required.j2"),
    }
    _log_branch_gate_outcome(gate, "block", None, None)
    return BranchStateGateDecision(gate=gate, outcome="block", payload=payload)


def _unpushed_protection_decision(
    repo: Path,
    facts: GitFacts,
    upstream: str,
    options: StopCheckOptions,
) -> BranchStateGateDecision | None:
    """Return a protected-branch decision before the ahead-of-upstream check."""
    gate = "unpushed_commits"
    local_protection = _current_branch_protection(
        repo, options.config.protected_branches
    )
    local_decision = _protected_branch_skip_decision(gate, local_protection, "local")
    if local_decision is not None:
        return local_decision
    upstream_protection = _tracked_branch_protection(
        repo,
        upstream,
        facts.primary_remote,
        options.config.protected_branches,
    )
    return _protected_branch_skip_decision(gate, upstream_protection, "upstream")


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
        return BranchStateGateDecision(gate=gate, outcome="skip")
    facts = state.git_facts
    if facts is None or facts.upstream_ref is None:
        return BranchStateGateDecision(gate=gate, outcome="pass")
    upstream = facts.upstream_ref
    protection = _unpushed_protection_decision(repo, facts, upstream, options)
    if protection is not None:
        return protection
    ahead, err = has_unpushed_commits(repo, upstream)
    if err is not None:
        return BranchStateGateDecision(
            gate=gate,
            outcome="pass",
            error=HookError("has_unpushed_commits", err),
        )
    if not ahead:
        return BranchStateGateDecision(gate=gate, outcome="pass")
    payload = {
        "decision": "block",
        "reason": render("unpushed_required.j2", upstream_ref=upstream),
    }
    _log_branch_gate_outcome(gate, "block", None, None)
    return BranchStateGateDecision(gate=gate, outcome="block", payload=payload)


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
            error=HookError("branch_protection", protection.error),
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
) -> int | None:
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
    int | None
        ``None`` when the gate is disabled, no checks ran, or checks passed,
        or ``0`` after emitting one blocking payload under the hook contract.

    """
    if not options.config.gate_quality_checks:
        return None

    selection = select_categories(state.changed_files)
    if not selection.targets_requested:
        return None

    match select_build_driver(repo, options):
        case HookError() as error:
            report = dataclasses.replace(
                state,
                ok=False,
                categories=selection.categories,
                targets_requested=list(selection.targets_requested),
                error=str(error),
            )
            return block_and_print(report)
        case None:
            _log_quality_gate_skip()
            return None
        case driver:
            evaluation = evaluate_changes(state, repo, options.max_out, driver)
            if not evaluation.blocked:
                return None
            return block_and_print(evaluation_report(state, evaluation))


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
    uncommitted_decision = uncommitted_changes_gate(repo, options)
    _log_branch_gate_error(uncommitted_decision)
    if uncommitted_decision.should_block:
        _emit_branch_gate_decision(uncommitted_decision)
        return 0
    unpushed_decision = unpushed_commits_gate(repo, state, options)
    _log_branch_gate_error(unpushed_decision)
    if unpushed_decision.should_block:
        _emit_branch_gate_decision(unpushed_decision)
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

    quality_result = run_quality_checks_if_needed(state, repo, options)
    if quality_result is not None:
        return quality_result

    return run_branch_state_gates(repo, state, options)
