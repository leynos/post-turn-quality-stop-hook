"""Decide the branch-state and pull-request rebase gates.

Every function here returns a :class:`~.state.BranchStateGateDecision` (or the
value the next step decides from) rather than printing, logging, or reporting.
A gate therefore describes what it decided without writing anywhere, and the
orchestration in :mod:`post_turn_quality_stop_hook.pipeline` owns the payload
emission and the observer events that follow.

Branch-state precedence is ``uncommitted_changes_gate``,
``unpushed_commits_gate``, then the pull-request rebase gate. The unpushed gate
is decided from state collected by :func:`refresh_upstream_head`, which is the
one step that consults the upstream port; the decision itself performs no
transport work.
"""

from __future__ import annotations

import dataclasses
import typing as typ

from post_turn_quality_stop_hook.driver import BuildDriver, get_build_targets
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.formatting import upstream_label
from post_turn_quality_stop_hook.git import (
    current_branch,
    ensure_remote_branch_ref,
    has_uncommitted_changes,
    head_commit,
    is_ancestor,
    merge_base,
    remote_url,
)
from post_turn_quality_stop_hook.github import PullRequestSummary, lookup_pr
from post_turn_quality_stop_hook.state import (
    BranchStateGateDecision,
    HookState,
    StopCheckOptions,
)
from post_turn_quality_stop_hook.templates import render

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.git import FetchObserver
    from post_turn_quality_stop_hook.git_facts import GitFacts
    from post_turn_quality_stop_hook.observer import CommandToken
    from post_turn_quality_stop_hook.upstream_port import (
        UpstreamComparison,
        UpstreamHeadSource,
    )

    #: Reports an actual ``git fetch`` the gate runs. The orchestrator binds it
    #: to the run's observer, so a decision never holds the observer itself.
    FetchReporter = FetchObserver[CommandToken | None]


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
        return BranchStateGateDecision(
            gate=gate,
            outcome="skip",
            matched_branch=local_protection.branch,
            match_kind="local",
            reason=f"protected branch {local_protection.branch} (local)",
        )
    dirty, err = has_uncommitted_changes(repo)
    if err is not None:
        return BranchStateGateDecision(
            gate=gate,
            outcome="pass",
            error=err,
            reason="git error",
        )
    if not dirty:
        return BranchStateGateDecision(
            gate=gate, outcome="pass", reason="working tree clean"
        )
    payload = {
        "decision": "block",
        "reason": render("uncommitted_required.j2"),
    }
    return BranchStateGateDecision(
        gate=gate,
        outcome="block",
        payload=payload,
        reason="uncommitted changes present",
    )


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
    work and the HEAD comparison; this function returns the value. Keeping this
    split lets ``unpushed_commits_gate`` stay a pure mapping from collected
    state to a decision.

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
        return BranchStateGateDecision(
            gate=gate, outcome="skip", reason="gate disabled by configuration"
        )

    preflight = _unpushed_preflight(repo, facts, options)
    if preflight is not None:
        return preflight

    comparison = source.refresh_comparison()
    if comparison is None:
        return BranchStateGateDecision(
            gate=gate, outcome="pass", reason="no upstream configured"
        )
    return comparison


def _unpushed_preflight(
    repo: Path, facts: GitFacts | None, options: StopCheckOptions
) -> BranchStateGateDecision | None:
    """Return a terminal decision, or ``None`` when the gate should proceed.

    Missing facts, an unreadable upstream, or a protected branch settles the
    gate here, before any transport work or comparison.
    """
    gate = "unpushed_commits"
    if facts is None:
        return BranchStateGateDecision(
            gate=gate, outcome="pass", reason="no upstream configured"
        )
    if facts.upstream_error is not None:
        return BranchStateGateDecision(
            gate=gate,
            outcome="pass",
            error=facts.upstream_error,
            reason="git error",
            warning=(
                f"unpushed gate could not resolve the upstream: {facts.upstream_error}"
            ),
        )
    if facts.upstream_remote is None or facts.upstream_branch is None:
        return BranchStateGateDecision(
            gate=gate, outcome="pass", reason="no upstream configured"
        )
    return _protected_upstream_decision(repo, facts, options, gate)


def _protected_upstream_decision(
    repo: Path, facts: GitFacts, options: StopCheckOptions, gate: str
) -> BranchStateGateDecision | None:
    """Skip the gate when either side of the comparison is protected.

    The local branch is read live, so a skip it produces always describes the
    checkout. The upstream comes from the collected snapshot instead, so its
    protection is applied only while that snapshot still describes the
    checked-out branch: a checkout that moved after collection would otherwise
    lend the current branch another branch's protected upstream and leave its
    own unpushed commits unchecked.
    """
    local_protection = _current_branch_protection(
        repo, options.config.protected_branches
    )
    local_decision = _protected_branch_skip_decision(gate, local_protection, "local")
    if local_decision is not None:
        return local_decision
    if not _snapshot_describes_checkout(facts, local_protection):
        return None
    return _protected_branch_skip_decision(
        gate,
        BranchProtectionCheck(
            is_protected=facts.upstream_branch in options.config.protected_branches,
            branch=facts.upstream_branch,
        ),
        "upstream",
    )


def _snapshot_describes_checkout(
    facts: GitFacts, live_protection: BranchProtectionCheck
) -> bool:
    """Whether the collected facts still describe the checked-out branch."""
    return facts.branch is not None and facts.branch == live_protection.branch


def unpushed_commits_gate(
    refresh: BranchStateGateDecision | UpstreamComparison,
) -> BranchStateGateDecision:
    """Decide the unpushed gate from collected upstream state.

    The refresh step has already resolved the upstream, fetched its head, and
    compared it with HEAD. This decision function performs no I/O, no port
    call, no rendering, and no logging, so it can be exercised with a prepared
    comparison and its result inspected directly. The orchestration renders
    and emits the block when the hook reports it.

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
            reason="git error",
            warning=(
                "unpushed gate could not verify the upstream "
                f"{upstream_label(refresh)}: {refresh.error}"
            ),
        )
    if not refresh.ahead:
        return BranchStateGateDecision(
            gate=gate, outcome="pass", reason="no unpushed commits"
        )
    return BranchStateGateDecision(
        gate=gate, outcome="block", reason="unpushed commits present"
    )


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
            reason="git error",
        )
    if not protection.is_protected:
        return None
    return BranchStateGateDecision(
        gate=gate,
        outcome="skip",
        matched_branch=protection.branch,
        match_kind=match_kind,
        reason=f"protected branch {protection.branch} ({match_kind})",
    )


@dataclasses.dataclass(slots=True, frozen=True)
class PrRebaseDecision:
    """The pull-request rebase gate's decision and its rendering inputs.

    Attributes
    ----------
    decision
        Gate outcome the orchestration records and emits. A block carries the
        rendered payload, so the decision itself stays free of printing.
    target_error
        Failure to read the Makefile's targets while preparing the block's
        typecheck hint. The orchestration reports it as an operator warning;
        a skip or pass leaves it ``None``.

    """

    decision: BranchStateGateDecision
    target_error: HookError | None = None


def pr_rebase_decision(
    repo: Path,
    state: HookState,
    options: StopCheckOptions,
    *,
    fetch_observer: FetchReporter,
) -> PrRebaseDecision:
    """Decide whether the open pull request's base is ahead of this branch.

    Parameters
    ----------
    repo
        Repository path.
    state
        Hook state containing collected Git facts.
    options
        Runtime options and configuration used for PR lookup and rendering.
    fetch_observer
        Reports the base fetch to the run's observer, if one runs.

    Returns
    -------
    PrRebaseDecision
        The gate outcome, its fixed reason, any rendered block payload, and an
        optional target-read failure the orchestration warns about.

    """
    outcome, context = _pr_rebase_outcome(repo, state, options, fetch_observer)
    if context is None:
        return PrRebaseDecision(
            decision=BranchStateGateDecision(
                gate="pr_rebase", outcome=outcome.kind, reason=outcome.reason
            )
        )
    facts, summary = context

    enumeration = get_build_targets(
        repo, BuildDriver("make", options.make_bin, "Makefile")
    )
    match enumeration.available:
        case HookError() as target_error:
            makefile_has_typecheck_target = False
        case available_targets:
            makefile_has_typecheck_target = "typecheck" in available_targets
            target_error = None
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
    return PrRebaseDecision(
        decision=BranchStateGateDecision(
            gate="pr_rebase",
            outcome="block",
            payload=payload,
            reason="pull request base is ahead",
        ),
        target_error=target_error,
    )


@dataclasses.dataclass(slots=True, frozen=True)
class _PrRebaseOutcome:
    """The gate's outcome and its fixed decision-log reason.

    Attributes
    ----------
    kind
        ``block`` when the base is ahead, ``pass`` when it is known not to be
        ahead, and ``skip`` when the comparison could not be determined.
    reason
        Fixed, output-free explanation recorded in the decision log.

    """

    kind: typ.Literal["pass", "skip", "block"]
    reason: str


def _pr_rebase_outcome(
    repo: Path,
    state: HookState,
    options: StopCheckOptions,
    fetch_observer: FetchReporter,
) -> tuple[_PrRebaseOutcome, tuple[GitFacts, PullRequestSummary] | None]:
    """Decide the gate, reporting every undetermined step as a skip.

    A pass is recorded only when the base is known not to be ahead. A disabled
    gate, a missing remote, an unreadable branch or URL, no open pull request,
    a failed base fetch, or an undeterminable ancestry is a skip with its
    reason, so a failed lookup is never mistaken for a passing check.
    """
    context = _pr_rebase_context(repo, state, options)
    match context:
        case _PrRebaseComparison(facts=facts, summary=summary) as comparison:
            outcome = _pr_base_is_ahead(
                repo, facts.primary_remote or "", comparison, fetch_observer
            )
            blocked = outcome.kind == "block"
            return outcome, ((facts, summary) if blocked else None)
        case _PrRebaseSkip(reason=reason):
            return _PrRebaseOutcome(kind="skip", reason=reason), None


@dataclasses.dataclass(slots=True, frozen=True)
class _PrCheckout:
    """The checkout a pull request lookup was made for.

    Attributes
    ----------
    branch
        Branch that was checked out when the lookup began.
    commit
        Commit ``HEAD`` named when the lookup began.

    """

    branch: str
    commit: str


@dataclasses.dataclass(slots=True, frozen=True)
class _PrRebaseComparison:
    """The PR the gate found, with the facts its prompt renders from.

    Attributes
    ----------
    facts
        Collected Git facts the pull request was looked up through.
    summary
        Located pull request whose base must be compared.
    checkout
        Branch and commit captured when the lookup began. The base is
        compared with this commit, so a checkout that moves during the fetch
        cannot change what the comparison means.

    """

    facts: GitFacts
    summary: PullRequestSummary
    checkout: _PrCheckout


@dataclasses.dataclass(slots=True, frozen=True)
class _PrRebaseSkip:
    """A step that left the gate undetermined, with its fixed reason.

    Attributes
    ----------
    reason
        Fixed, output-free explanation recorded in the decision log.

    """

    reason: str


def _pr_rebase_context(  # ruff:ignore[too-many-return-statements]  # each undetermined step reports its own fixed reason.
    repo: Path,
    state: HookState,
    options: StopCheckOptions,
) -> _PrRebaseComparison | _PrRebaseSkip:
    """Locate the open pull request, or report why the gate cannot decide.

    The gate is undetermined when it is disabled, no remote or branch is known,
    the remote URL cannot be read, or no open pull request matches. Each case
    names its own fixed reason so the record explains the skip rather than
    reporting a generic failure. The base fetch happens after this lookup and is
    reported through the orchestrator's fetch reporter.
    """
    if not options.config.gate_pr_rebase:
        return _PrRebaseSkip(reason="gate disabled by configuration")
    facts = _pr_rebase_facts(options, state.git_facts)
    if facts is None:
        return _PrRebaseSkip(reason="no primary remote")
    remote = facts.primary_remote
    if remote is None:
        return _PrRebaseSkip(reason="no primary remote")
    branch, _err = current_branch(repo)
    if branch is None:
        return _PrRebaseSkip(reason="could not determine the current branch")
    commit, _err = head_commit(repo)
    if commit is None:
        return _PrRebaseSkip(reason="could not determine the current commit")
    url, _err = remote_url(repo, remote)
    if url is None:
        return _PrRebaseSkip(reason="could not determine the remote URL")
    summary = lookup_pr(url, branch, timeout=options.config.github_timeout_seconds)
    if summary is None:
        return _PrRebaseSkip(reason="could not determine an open pull request")
    return _PrRebaseComparison(
        facts=facts,
        summary=summary,
        checkout=_PrCheckout(branch=branch, commit=commit),
    )


def _pr_rebase_facts(
    options: StopCheckOptions, facts: GitFacts | None
) -> GitFacts | None:
    """Return the git facts when the PR rebase gate applies, otherwise None.

    The gate applies when it is enabled and a primary remote is known.
    """
    if not options.config.gate_pr_rebase or facts is None:
        return None
    return facts if facts.primary_remote else None


def _pr_base_is_ahead(
    repo: Path,
    remote: str,
    comparison: _PrRebaseComparison,
    fetch_observer: FetchReporter,
) -> _PrRebaseOutcome:
    """Decide whether the PR base has commits beyond the local merge base.

    The remote base branch is fetched first. A failed fetch, an unresolvable
    merge base, or an undeterminable ancestry leaves the comparison
    *undetermined*: it is recorded as a skip with its fixed reason, so a failed
    lookup is never mistaken for a passing check. Only a base that is known
    not to be ahead is a pass. The fetch is a blocking command, so it is
    reported through ``fetch_observer`` with its own exit code.

    The merge base is computed against the commit captured when the lookup
    began, not live ``HEAD``, so a checkout that moves during the fetch cannot
    change what is compared. A pass or block is reported only while that
    branch and commit are still checked out; otherwise the gate is
    undetermined and says so.
    """
    summary = comparison.summary
    checkout = comparison.checkout
    ok, _err, _fetched = ensure_remote_branch_ref(
        repo,
        remote,
        summary.base_branch,
        always_fetch=True,
        observer=fetch_observer,
    )
    if not ok:
        return _PrRebaseOutcome(
            kind="skip", reason="could not fetch the pull request base"
        )

    local_base, _err = merge_base(
        repo, f"{remote}/{summary.base_branch}", commit=checkout.commit
    )
    if local_base is None:
        return _PrRebaseOutcome(
            kind="skip", reason="could not compute the pull request merge base"
        )
    outcome = _pr_base_relation(repo, local_base, summary.base_oid)
    if outcome.kind == "skip":
        return outcome
    if not _checkout_is(repo, checkout):
        return _PrRebaseOutcome(
            kind="skip", reason="the checkout changed during the pull request check"
        )
    return outcome


def _pr_base_relation(repo: Path, local_base: str, base_oid: str) -> _PrRebaseOutcome:
    """Relate the local merge base to the pull request's base commit."""
    if local_base == base_oid:
        return _PrRebaseOutcome(kind="pass", reason="pull request base is not ahead")
    ancestor, _err = is_ancestor(repo, local_base, base_oid)
    if ancestor is None:
        return _PrRebaseOutcome(
            kind="skip", reason="could not compare the pull request base"
        )
    if not ancestor:
        return _PrRebaseOutcome(kind="pass", reason="pull request base is not ahead")
    return _PrRebaseOutcome(kind="block", reason="pull request base is ahead")


def _checkout_is(repo: Path, checkout: _PrCheckout) -> bool:
    """Whether the captured branch and commit are still checked out."""
    branch, _err = current_branch(repo)
    if branch is None or branch != checkout.branch:
        return False
    commit, _err = head_commit(repo)
    return commit is not None and commit == checkout.commit
