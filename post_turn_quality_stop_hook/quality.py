"""Compose a quality evaluation from its target and command units.

Detects change categories, resolves them to declared build targets, runs the
resulting target groups, and returns immutable stage results. The pipeline
orchestrator owns stdout emission and blocking decisions.

The work is split by responsibility: :mod:`post_turn_quality_stop_hook.\
quality_targets` answers what could run, :mod:`post_turn_quality_stop_hook.\
quality_execution` runs it, and the reporting helpers in
:mod:`post_turn_quality_stop_hook.observation` speak the run's
:class:`~.observer.PipelineObserver`. That protocol is the stage's only
telemetry channel: an operator warning goes through the observation helpers'
warning channel and never through the standard-library logger.

Reporting is a keyword-only argument on the stage, so a unit test decides the
gate from prepared answers without an observer; the orchestrator supplies the
run's observer, which is a no-op when the decision log is disabled.
"""

from __future__ import annotations

import dataclasses
import typing as typ

from post_turn_quality_stop_hook import observation
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.quality_execution import execute_quality_targets
from post_turn_quality_stop_hook.quality_stage import QualityStage
from post_turn_quality_stop_hook.quality_targets import (
    enumerate_quality_targets,
    select_categories,
)
from post_turn_quality_stop_hook.state import (
    CategorySelection,
    HookState,
    QualityEvaluation,
    TargetSelection,
)

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook.quality_policy import CommandResult

__all__ = [
    "QualityStage",
    "enumerate_quality_targets",
    "evaluate_changes",
    "evaluation_report",
    "execute_quality_targets",
    "select_categories",
]


def evaluate_changes(
    stage: QualityStage, selection: CategorySelection
) -> QualityEvaluation:
    """Compose quality units from an existing category selection.

    Parameters
    ----------
    stage
        Repository, driver, limits, and the injected ports.
    selection
        Categories and requested targets already resolved by the caller.

    Returns
    -------
    QualityEvaluation
        Immutable category, target and command results.

    """
    if not selection.targets_requested:
        return QualityEvaluation(
            categories=selection.categories,
            build_driver=stage.driver.name,
        )

    # Publish what is already known before enumeration starts. ``netsuke
    # generate`` is a blocking command, so a signal during it must still leave
    # the chosen driver and the requested targets in the finish record rather
    # than reporting an empty selection.
    _observe_quality(stage, selection, None, ())

    match enumerate_quality_targets(stage, selection):
        case HookError() as error:
            _report_enumeration_failure(stage, error)
            _observe_quality(stage, selection, None, ())
            return QualityEvaluation(
                categories=selection.categories,
                build_driver=stage.driver.name,
                targets_requested=selection.targets_requested,
                error=error,
            )
        case targets:
            commands = execute_quality_targets(stage, targets)
            _observe_quality(stage, selection, targets, commands)
            return QualityEvaluation(
                categories=selection.categories,
                build_driver=stage.driver.name,
                targets_requested=targets.targets_requested,
                targets_present=targets.targets_present,
                targets_skipped=targets.targets_skipped,
                commands=commands,
            )


def _report_enumeration_failure(stage: QualityStage, error: HookError) -> None:
    """Report a target read failure through the stage's observer.

    The warning is operator-facing rather than durable telemetry, so it goes
    through the observer's warning channel. A stage built without an observer
    reports nothing, which is the same contract as its other observations.
    """
    if stage.observer is not None:
        observation.warn_build_targets_unavailable(stage.observer, error)


def _observe_quality(
    stage: QualityStage,
    selection: CategorySelection,
    targets: TargetSelection | None,
    commands: tuple[CommandResult, ...],
) -> None:
    """Publish the selection facts once the evaluation is settled."""
    if stage.observer is None:
        return
    observation.publish_quality_selection(
        stage.observer,
        observation.quality_observation_fields(
            build_driver=stage.driver.name,
            build_driver_reason=stage.driver.selection_reason,
            targets_requested=selection.targets_requested,
            targets=targets,
            commands=commands,
        ),
    )


def evaluation_report(state: HookState, evaluation: QualityEvaluation) -> HookState:
    """Build a reporting snapshot from a quality evaluation.

    Parameters
    ----------
    state
        Snapshot to copy.
    evaluation
        Quality-stage result whose fields populate the new snapshot.

    Returns
    -------
    HookState
        New snapshot with ``ok`` false when the evaluation blocks.

    """
    return dataclasses.replace(
        state,
        ok=not evaluation.blocked,
        categories=evaluation.categories,
        build_driver=evaluation.build_driver,
        targets_requested=evaluation.targets_requested,
        targets_present=evaluation.targets_present,
        targets_run=evaluation.targets_present,
        targets_skipped=evaluation.targets_skipped,
        commands=evaluation.commands,
        error=None if evaluation.error is None else str(evaluation.error),
    )
