"""Stop-hook orchestration pipeline.

Coordinates repository preparation, change detection, build-driver
selection, target execution, and the final pass/block decision. The pieces it
composes are separate units:

- :mod:`post_turn_quality_stop_hook.branch_gates` decides the branch-state and
  pull-request rebase gates, returning values rather than printing;
- :mod:`post_turn_quality_stop_hook.quality` composes target selection and
  command execution from its ports;
- :mod:`post_turn_quality_stop_hook.formatting` renders the JSON payloads;
- :mod:`post_turn_quality_stop_hook.observation` speaks the observer protocol.

This module owns the run's order, its stage boundaries, and the emission that
follows a decision. Entry points: ``prepare_run_stop_checks`` and
``run_stop_checks``.

Every decision-relevant event — a gate outcome, the quality selection, the
commands a stage runs, a fixed operator warning, and whether the run blocked —
is reported through the run's :class:`~.observer.PipelineObserver`. The
observer is built once at the ``run_stop_checks`` boundary from the optional
decision-log recorder, so no gate holds the log itself. Every function that
reports takes an observer, with no silent no-op default: a caller that does not
care about observation passes a ``NullObserver`` explicitly, with the same
warning channel the composition root binds.
"""

from __future__ import annotations

import dataclasses
import functools
import typing as typ

from post_turn_quality_stop_hook import formatting, observation
from post_turn_quality_stop_hook.branch_gates import (
    PrRebaseDecision,
    pr_rebase_decision,
    refresh_upstream_head,
    uncommitted_changes_gate,
    unpushed_commits_gate,
)
from post_turn_quality_stop_hook.driver import (
    BuildDriver,
    _is_executable_available,
    get_build_targets,
    select_build_driver,
)
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.execution import run_build_targets
from post_turn_quality_stop_hook.fetch_observation import ObserverFetchObserver
from post_turn_quality_stop_hook.formatting import block_and_print
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
from post_turn_quality_stop_hook.observer import observer_for
from post_turn_quality_stop_hook.quality import (
    QualityStage,
    evaluate_changes,
    evaluation_report,
    select_categories,
)
from post_turn_quality_stop_hook.recording import InvocationLog
from post_turn_quality_stop_hook.repository_facts import observe_repository_facts
from post_turn_quality_stop_hook.state import (
    BranchStateGateDecision,
    CategorySelection,
    HookState,
    PreparationBlock,
    PreparationReady,
    PreparationStop,
    QualityEvaluation,
    StopCheckOptions,
)
from post_turn_quality_stop_hook.upstream_port import UpstreamComparison

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from post_turn_quality_stop_hook.branch_state_port import BranchGatePorts
    from post_turn_quality_stop_hook.config import Config
    from post_turn_quality_stop_hook.git_facts import GitFacts
    from post_turn_quality_stop_hook.observer import PipelineObserver
    from post_turn_quality_stop_hook.repository_facts import RepositoryFactsSource
    from post_turn_quality_stop_hook.upstream_port import UpstreamHeadSource

    #: Writes one fixed, output-free diagnostic for the operator. The entry
    #: point binds the standard-error writer, so neither observer reaches a
    #: stream itself.
    Warn = cabc.Callable[[str], None]

# The gate decisions a caller may import for their own reporting.
__all__ = [
    "BranchStateGateDecision",
    "pr_rebase_check",
    "prepare_run_stop_checks",
    "refresh_upstream_head",
    "run_branch_state_gates",
    "run_quality_checks_if_needed",
    "run_stop_checks",
    "uncommitted_changes_gate",
    "unpushed_commits_gate",
]


def _blocked_preparation(
    state: HookState,
    error: HookError,
    *,
    reason: str,
    repo: Path | None = None,
) -> PreparationBlock:
    """Return a blocking preparation outcome for an immutable snapshot.

    ``reason`` is the fixed decision-log explanation; the failure's message is
    kept only on the state, where it is rendered for the user.
    """
    return PreparationBlock(
        state=dataclasses.replace(state, ok=False, error=str(error)),
        repo=repo,
        error=error,
        reason=reason,
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
        reason="could not resolve repository root",
    )


def prepare_run_stop_checks(  # ruff:ignore[too-many-arguments]  # start directory, base ref, fetch policy, config and observer are distinct inputs.
    start_cwd: Path,
    base_ref: str,
    *,
    always_fetch: bool,
    config: Config,
    observer: PipelineObserver,
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
    observer
        Receives any fetch command the base-ref check actually runs. It is
        required, so no caller silently drops the fetch it performed; a caller
        that does not observe passes a ``NullObserver``.

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
            reason="git not found on PATH",
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
        observer=ObserverFetchObserver(observer),
    )
    state = dataclasses.replace(state, fetched=fetched)
    if not ok:
        return _blocked_preparation(
            state,
            err or HookError("ensure_base_ref", "base ref unavailable"),
            reason="could not ensure base ref",
            repo=repo,
        )

    base_commit, err = merge_base(repo, base_ref)
    if base_commit is None:
        return _blocked_preparation(
            state,
            err or HookError("merge_base", "merge base unavailable"),
            reason="could not find merge base",
            repo=repo,
        )
    state = dataclasses.replace(state, base_commit=base_commit)

    files, err = changed_files(repo, base_commit)
    if files is None:
        return _blocked_preparation(
            state,
            err or HookError("changed_files", "changed files unavailable"),
            reason="could not list changed files",
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
    upstream_name = upstream or "origin (upstream not configured)"

    dirty, err = has_uncommitted_changes(repo)
    if err is not None:
        return 0
    if dirty:
        formatting.emit_block_payload(f"Please commit and push to {upstream_name}")
        return 0

    if upstream is None:
        return 0

    ahead, err = has_unpushed_commits(repo, upstream)
    if err is not None or not ahead:
        return 0

    formatting.emit_block_payload(f"Please push committed changes to {upstream_name}")
    return 0


def pr_rebase_check(  # ruff:ignore[too-many-arguments]  # repository, state, options, observer and gate ports are distinct inputs.
    repo: Path,
    state: HookState,
    options: StopCheckOptions,
    *,
    observer: PipelineObserver,
    ports: BranchGatePorts,
) -> int:
    """Block the stop when the open pull request base is ahead of this branch.

    The decision comes from :func:`~.branch_gates.pr_rebase_decision`; this
    function reports the outcome through the observer and emits the payload.
    It is also where the gate's zero-argument target reader is bound, so the
    gate never imports the driver adapter.

    Parameters
    ----------
    repo
        Repository path.
    state
        Hook state containing collected Git facts.
    options
        Runtime options and configuration used for PR lookup.
    observer
        Receives the gate's outcome and the base fetch it runs. It is required,
        so a caller cannot drop the outcome this gate is the only reporter of.
    ports
        Gate adapters the composition root bound: the branch-state questions,
        the pull-request lookup with its timeout, and the target reader.

    Returns
    -------
    int
        Hook exit code, always ``0`` per the stop-hook contract.

    """
    outcome: PrRebaseDecision = pr_rebase_decision(
        repo,
        state,
        options,
        branch_state=ports.branch_state,
        lookup_pr=ports.lookup_pull_request,
        enumerate_targets=functools.partial(
            ports.enumerate_build_targets,
            repo,
            BuildDriver("make", options.make_bin, "Makefile"),
        ),
        fetch_observer=ObserverFetchObserver(observer),
    )
    if outcome.target_error is not None:
        observation.warn_build_targets_unavailable(observer, outcome.target_error)
    observation.record_gate_decision(observer, outcome.decision)
    formatting.emit_decision_payload(outcome.decision)
    return 0


def run_quality_checks_if_needed(
    state: HookState,
    repo: Path,
    options: StopCheckOptions,
    *,
    observer: PipelineObserver,
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
    observer
        Receives the quality gate's decision, the selection facts, and the
        commands the stage runs. It is required, so no caller silently drops
        the gate outcome; a caller that does not observe passes a
        ``NullObserver``.

    Returns
    -------
    HookState | None
        A blocking report when checks fail or a driver cannot be selected, or
        ``None`` when the gate is disabled, no checks ran, or checks passed.
        Emission belongs to ``run_stop_checks``.

    """
    if not options.config.gate_quality_checks:
        observation.record_quality_decision(
            observer, "quality_checks", "skip", "gate disabled by configuration"
        )
        return None

    selection = select_categories(state.changed_files)
    if not selection.targets_requested:
        observation.record_quality_decision(
            observer, "quality_checks", "skip", "no quality targets for changed files"
        )
        return None

    match select_build_driver(repo, options):
        case HookError() as error:
            _publish_requested_targets(observer, selection)
            observation.record_quality_decision(
                observer, "quality_checks", "block", "could not select build driver"
            )
            # The error names the requested driver, which comes from the
            # environment, so the response keeps the detail while the record
            # keeps the fixed reason above.
            return dataclasses.replace(
                state,
                ok=False,
                categories=selection.categories,
                targets_requested=selection.targets_requested,
                error=str(error),
            )
        case None:
            _publish_requested_targets(observer, selection)
            observation.record_quality_decision(
                observer,
                "quality_checks",
                "skip",
                "no supported build driver available",
            )
            return None
        case driver:
            stage = QualityStage(
                repo=repo,
                driver=driver,
                max_out=options.max_out,
                enumerate_targets=get_build_targets,
                run_commands=run_build_targets,
                observer=observer,
            )
            evaluation = evaluate_changes(stage, selection)
            _record_quality_outcome(observer, evaluation)
            return (
                None if not evaluation.blocked else evaluation_report(state, evaluation)
            )


def _publish_requested_targets(
    observer: PipelineObserver, selection: CategorySelection
) -> None:
    """Publish the request list when driver selection stops the gate.

    Driver selection can fail or find no supported driver before any target is
    enumerated. The request list is still known, so it is published without a
    selected driver or enumerated targets; otherwise the record cannot
    distinguish "targets were requested but could not run" from "no targets
    were requested".
    """
    observation.publish_quality_selection(
        observer,
        observation.quality_observation_fields(
            build_driver="",
            build_driver_reason="",
            targets_requested=selection.targets_requested,
            targets=None,
            commands=(),
        ),
    )


def _record_quality_outcome(
    observer: PipelineObserver, evaluation: QualityEvaluation
) -> None:
    """Record the quality gate outcome one evaluation settled on.

    A passed gate is recorded only when a requested target actually ran. When
    enumeration succeeded but the driver declares none of the requested
    targets, no command ran, so the gate is recorded as a skip stating that.
    The fixed "quality checks passed" text would otherwise claim checks that
    never executed. Blocking decisions and output are unaffected.
    """
    if evaluation.error is not None:
        observation.record_quality_decision(
            observer,
            "quality_checks",
            "block",
            "could not enumerate build targets",
        )
        return
    if evaluation.blocked:
        observation.record_quality_decision(
            observer,
            "quality_checks",
            "block",
            "quality checks failed",
        )
        return
    if not evaluation.commands:
        observation.record_quality_decision(
            observer,
            "quality_checks",
            "skip",
            "no requested target is declared by the build driver",
        )
        return
    observation.record_quality_decision(
        observer, "quality_checks", "pass", "quality checks passed"
    )


def run_branch_state_gates(  # ruff:ignore[too-many-arguments]  # repo, state, options, the upstream port, the gate ports and the observer are distinct inputs.
    repo: Path,
    state: HookState,
    options: StopCheckOptions,
    upstream_source: UpstreamHeadSource,
    *,
    observer: PipelineObserver,
    ports: BranchGatePorts,
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
    observer
        Receives each gate's stage boundary and decision, and is passed to the
        final gate so its own outcome and base fetch are recorded too. It is
        required, so no caller silently drops a gate outcome.
    ports
        Gate adapters the composition root bound.

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
    with observation.record_stage_start(observer, "gate_uncommitted_changes"):
        uncommitted_decision = uncommitted_changes_gate(
            repo, options, branch_state=ports.branch_state
        )
    observation.record_gate_decision(observer, uncommitted_decision)
    if uncommitted_decision.should_block:
        formatting.emit_decision_payload(uncommitted_decision)
        return 0
    with observation.record_stage_start(observer, "gate_unpushed_commits"):
        refresh = refresh_upstream_head(
            repo,
            state.git_facts,
            options,
            upstream_source,
            branch_state=ports.branch_state,
        )
        unpushed_decision = unpushed_commits_gate(refresh)
    observation.record_gate_decision(observer, unpushed_decision)
    if unpushed_decision.should_block:
        if isinstance(refresh, UpstreamComparison):
            formatting.emit_unpushed_block(refresh)
        else:
            formatting.emit_decision_payload(unpushed_decision)
        return 0
    with observation.record_stage_start(observer, "gate_pr_rebase"):
        # The rebase check can block in a network fetch. The two earlier
        # decisions were recorded above, before the fetch starts, so a signal
        # that interrupts it still leaves a finish record naming them rather
        # than reporting passed gates as never reached. The run's observer is
        # passed on so the fetch and the gate's own outcome are recorded too.
        return pr_rebase_check(repo, state, options, observer=observer, ports=ports)


def run_stop_checks(  # ruff:ignore[too-many-arguments]  # working directory, base ref, options, adapters and recorder are distinct inputs.
    start_cwd: Path,
    base_ref: str,
    options: StopCheckOptions,
    upstream_source_for: cabc.Callable[
        [Path, GitFacts | None, PipelineObserver], UpstreamHeadSource
    ],
    *,
    branch_gate_ports: BranchGatePorts,
    repository_facts_source: RepositoryFactsSource,
    invocation: InvocationLog | None = None,
    warn: Warn,
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
        Builds the upstream port for the prepared repository, its facts, and
        the run's observer. The entry point supplies it, which keeps this
        module free of the upstream adapter and lets a caller substitute
        another implementation.
    branch_gate_ports
        Branch-state, pull-request and build-target adapters the entry point
        bound. They keep the gate module free of Git, GitHub, driver and
        template imports, and let a test decide every gate from prepared
        answers.
    repository_facts_source
        Query port for the log-only repository facts. The entry point supplies
        the Git adapter, so the observation's domain unit depends only on the
        port and a test can supply prepared answers.
    invocation
        Decision-log recorder for this run, if one is active. It is read here
        and nowhere else: the pipeline receives only the observer built from
        it, so gate functions never see the recorder.
    warn
        Writes one fixed warning for the operator. The entry point binds the
        standard-error writer, so an observer never reaches a stream itself
        and a run with logging disabled reports a warning exactly as a
        recording run does.

    Returns
    -------
    int
        Exit code for the hook.

    """
    observer = observer_for(invocation or InvocationLog(), warn=warn)
    with observation.record_stage_start(observer, "prepare"):
        match prepare_run_stop_checks(
            start_cwd,
            base_ref,
            always_fetch=options.always_fetch,
            config=options.config,
            observer=observer,
        ):
            case PreparationStop():
                return 0
            case PreparationBlock() as blocked:
                observation.mark_blocked(
                    observer, blocked.reason or "preparation failed"
                )
                return block_and_print(blocked.state)
            case PreparationReady(state=state, repo=repo):
                pass

    # Facts exist only for the durable record; observing them here keeps
    # preparation free of log-only work, and a failure inside the observer
    # is reported rather than aborting the run.
    observe_repository_facts(
        observer, repository_facts_source, repo, base_ref, state.git_facts
    )

    with observation.record_stage_start(observer, "quality_checks"):
        quality_report = run_quality_checks_if_needed(
            state, repo, options, observer=observer
        )
    if quality_report is not None:
        return block_and_print(quality_report)

    upstream_source = upstream_source_for(repo, state.git_facts, observer)
    with observation.record_stage_start(observer, "branch_state_gates"):
        return run_branch_state_gates(
            repo,
            state,
            options,
            upstream_source,
            observer=observer,
            ports=branch_gate_ports,
        )
