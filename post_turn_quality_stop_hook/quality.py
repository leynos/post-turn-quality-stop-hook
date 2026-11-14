"""Quality-check stage for changed files.

Detects change categories, resolves them to declared build targets, runs
the resulting target groups, and returns immutable stage results. The
pipeline orchestrator owns stdout emission and blocking decisions.
"""

from __future__ import annotations

import dataclasses
import logging
import typing as typ

from post_turn_quality_stop_hook.driver import BuildDriver, get_build_targets
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.execution import (
    CODE_CATS,
    MD_CATS,
    BuildTargetRequest,
    CommandResult,
    run_build_targets,
    select_targets,
    targets_for_categories,
)
from post_turn_quality_stop_hook.formatting import detect_categories
from post_turn_quality_stop_hook.state import (
    CategorySelection,
    HookState,
    QualityEvaluation,
    TargetSelection,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def select_categories(changed_files: list[str]) -> CategorySelection:
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
    repo: Path, driver: BuildDriver, selection: CategorySelection
) -> TargetSelection | HookError:
    """Enumerate declared targets and select the requested ones.

    Parameters
    ----------
    repo
        Repository root path.
    driver
        Build driver selected for quality gates.
    selection
        Detected categories and requested targets.

    Returns
    -------
    TargetSelection | HookError
        Present and missing targets, or an enumeration failure.

    """
    match get_build_targets(repo, driver):
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
    repo: Path,
    driver: BuildDriver,
    selection: TargetSelection,
    max_out: int,
) -> tuple[CommandResult, ...]:
    """Run non-empty code and Markdown target groups in order.

    Parameters
    ----------
    repo
        Repository root path.
    driver
        Build driver selected for quality gates.
    selection
        Present targets grouped by category.
    max_out
        Maximum number of output characters to capture.

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
        run_build_targets(
            repo,
            BuildTargetRequest(driver, kind, list(targets)),
            max_out,
        )
        for kind, targets in groups
        if targets
    )


def evaluate_changes(
    state: HookState, repo: Path, max_out: int, driver: BuildDriver
) -> QualityEvaluation:
    """Compose quality units for the files recorded in ``state``.

    Parameters
    ----------
    state
        Hook reporting snapshot; only ``changed_files`` is read.
    repo
        Repository root path.
    max_out
        Maximum number of output characters to capture.
    driver
        Build driver selected for quality gates.

    Returns
    -------
    QualityEvaluation
        Immutable category, target and command results.

    """
    selection = select_categories(state.changed_files)
    if not selection.targets_requested:
        return QualityEvaluation(
            categories=selection.categories,
            build_driver=driver.name,
        )

    match enumerate_quality_targets(repo, driver, selection):
        case HookError() as error:
            logger.warning("Could not enumerate build targets: %s", error)
            return QualityEvaluation(
                categories=selection.categories,
                build_driver=driver.name,
                targets_requested=selection.targets_requested,
                error=error,
            )
        case targets:
            return QualityEvaluation(
                categories=selection.categories,
                build_driver=driver.name,
                targets_requested=targets.targets_requested,
                targets_present=targets.targets_present,
                targets_skipped=targets.targets_skipped,
                commands=execute_quality_targets(repo, driver, targets, max_out),
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
        categories=set(evaluation.categories),
        build_driver=evaluation.build_driver,
        targets_requested=list(evaluation.targets_requested),
        targets_present=list(evaluation.targets_present),
        targets_run=list(evaluation.targets_present),
        targets_skipped=list(evaluation.targets_skipped),
        commands=list(evaluation.commands),
        error=None if evaluation.error is None else str(evaluation.error),
    )
