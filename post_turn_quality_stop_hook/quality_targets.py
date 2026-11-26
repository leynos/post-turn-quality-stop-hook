"""Detect change categories and select the build targets they request.

This unit answers *what could run*: it maps changed files to categories, reads
the driver's declared targets through the stage's enumerator, and resolves the
requested targets into the present, skipped, and grouped lists an evaluation
executes. It never runs a build group: that belongs to
:mod:`post_turn_quality_stop_hook.quality_execution`, so a selection can be
tested without a subprocess.

``netsuke generate`` is itself a blocking command, so the one command this unit
runs is reported through :mod:`post_turn_quality_stop_hook.observation` with the
status the enumerator carried. A Makefile read runs no command and records
none.
"""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook import observation
from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.quality_policy import (
    CODE_CATS,
    MD_CATS,
    detect_categories,
    select_targets,
    targets_for_categories,
)
from post_turn_quality_stop_hook.state import CategorySelection, TargetSelection

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from post_turn_quality_stop_hook.driver import TargetEnumeration
    from post_turn_quality_stop_hook.quality_stage import QualityStage


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


def _enumerate_targets(stage: QualityStage) -> TargetEnumeration:
    """Read the driver's declared targets, reporting a blocking invocation.

    ``netsuke generate`` is a blocking subprocess. The executable can come from
    ``POST_TURN_NETSUKE_BIN``, so the record names the driver instead and keeps
    the environment-derived path out of it. A make manifest is parsed without
    running a command, so it produces no command record.

    The finish record carries the status the enumeration actually observed,
    which is the child's own status or the shared launch-failure value when no
    child started.
    """
    observer = stage.observer
    token = None
    if observer is not None and stage.driver.name == "netsuke":
        token = observation.start_command(
            observer, "targets", f"{stage.driver.name} generate", []
        )
    enumeration = stage.enumerate_targets(stage.repo, stage.driver)
    if observer is not None and token is not None:
        observation.finish_command(observer, token, enumeration.recorded_exit_status)
    return enumeration


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
    match _enumerate_targets(stage).available:
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
                # Keep the whole enumeration for the record. ``targets_present``
                # stays the requested subset the run selects and executes; a
                # code-only change would otherwise report a declared ``release``
                # target as unavailable.
                targets_available=tuple(sorted(available)),
            )
