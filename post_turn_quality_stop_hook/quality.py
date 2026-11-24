"""Quality-check stage for changed files.

Detects change categories, resolves them to declared build targets, runs
the resulting target groups, and returns immutable stage results. The
pipeline orchestrator owns stdout emission and blocking decisions.
"""

from __future__ import annotations

import dataclasses
import logging
import typing as typ

from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.quality_policy import (
    CODE_CATS,
    MD_CATS,
    BuildTargetRequest,
    CommandResult,
    detect_categories,
    select_targets,
    targets_for_categories,
)
from post_turn_quality_stop_hook.state import (
    CategorySelection,
    HookState,
    QualityEvaluation,
    TargetSelection,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from post_turn_quality_stop_hook.driver import BuildDriver

logger = logging.getLogger(__name__)


@dataclasses.dataclass(slots=True, frozen=True)
class QualityStage:
    """Adapters and limits needed to evaluate one repository state.

    Attributes
    ----------
    repo
        Repository root path.
    driver
        Build driver selected for quality gates.
    max_out
        Maximum number of output characters to capture per command.
    enumerate_targets
        Port that reads the driver's declared targets, reporting a read failure
        as ``HookError``.
    run_commands
        Port that runs one grouped target request and returns its result.

    """

    repo: Path
    driver: BuildDriver
    max_out: int
    enumerate_targets: cabc.Callable[[Path, BuildDriver], set[str] | HookError]
    run_commands: cabc.Callable[[Path, BuildTargetRequest, int], CommandResult]


def select_categories(changed_files: cabc.Iterable[str]) -> CategorySelection:
    """Detect change categories and the build targets they request.

    Parameters
    ----------
    changed_files
        Files changed relative to the diff base.

    Returns
    -------
    CategorySelection
        Detected categories and their requested targets.

    """
    categories = frozenset(detect_categories(changed_files))
    return CategorySelection(
        categories=categories,
        targets_requested=tuple(targets_for_categories(categories)),
    )


def enumerate_quality_targets(
    stage: QualityStage, selection: CategorySelection
) -> TargetSelection | HookError:
    """Enumerate declared targets and select the requested ones.

    Parameters
    ----------
    stage
        Repository, driver, limits, and the injected enumerator.
    selection
        Detected categories and requested targets.

    Returns
    -------
    TargetSelection | HookError
        Present and missing targets, or an enumeration failure.

    """
    match stage.enumerate_targets(stage.repo, stage.driver):
        case HookError() as error:
            return error
        case available:
            present = select_targets(selection.categories, available)
            return TargetSelection(
                targets_requested=selection.targets_requested,
                targets_present=tuple(present),
                targets_skipped=tuple(
                    target
                    for target in selection.targets_requested
                    if target not in available
                ),
                code_targets=tuple(
                    target
                    for target in targets_for_categories(
                        selection.categories, include=CODE_CATS
                    )
                    if target in available
                ),
                markdown_targets=tuple(
                    target
                    for target in targets_for_categories(
                        selection.categories, include=MD_CATS
                    )
                    if target in available
                ),
            )


def execute_quality_targets(
    stage: QualityStage, selection: TargetSelection
) -> tuple[CommandResult, ...]:
    """Run non-empty code and Markdown target groups in order.

    Parameters
    ----------
    stage
        Repository, driver, limits, and the injected command runner.
    selection
        Present targets grouped by category.

    Returns
    -------
    tuple[CommandResult, ...]
        Results for every non-empty group, in code-then-Markdown order.

    """
    groups = (
        ("code", selection.code_targets),
        ("markdown", selection.markdown_targets),
    )
    return tuple(
        stage.run_commands(
            stage.repo,
            BuildTargetRequest(stage.driver, kind, list(targets)),
            stage.max_out,
        )
        for kind, targets in groups
        if targets
    )


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

    match enumerate_quality_targets(stage, selection):
        case HookError() as error:
            logger.warning("Could not enumerate build targets: %s", error)
            return QualityEvaluation(
                categories=selection.categories,
                build_driver=stage.driver.name,
                targets_requested=selection.targets_requested,
                error=error,
            )
        case targets:
            return QualityEvaluation(
                categories=selection.categories,
                build_driver=stage.driver.name,
                targets_requested=targets.targets_requested,
                targets_present=targets.targets_present,
                targets_skipped=targets.targets_skipped,
                commands=execute_quality_targets(stage, targets),
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
