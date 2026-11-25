"""Quality-check stage for changed files.

Detects change categories, resolves them to declared build targets, runs
the resulting target groups, and returns immutable stage results. The
pipeline orchestrator owns stdout emission and blocking decisions.

The stage reports the commands it runs and the selection it made through the
run's :class:`~.observer.PipelineObserver`. Reporting is a keyword-only
argument, so a unit test decides the gate from prepared answers without an
observer; the orchestrator supplies the run's observer, which is a no-op when
the decision log is disabled.
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
    from post_turn_quality_stop_hook.observer import CommandToken, PipelineObserver

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
        Port that reads the driver's declared targets. For Netsuke it runs
        ``netsuke generate``; the read failure is reported as ``HookError``.
    run_commands
        Port that runs one grouped target request and returns its result.
    observer
        Receives the commands the stage runs and the selection it makes.
        ``None`` records nothing, so a unit test needs no observation seam.

    """

    repo: Path
    driver: BuildDriver
    max_out: int
    enumerate_targets: cabc.Callable[[Path, BuildDriver], set[str] | HookError]
    run_commands: cabc.Callable[[Path, BuildTargetRequest, int], CommandResult]
    observer: PipelineObserver | None = None


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


def _enumerate_targets(stage: QualityStage) -> set[str] | HookError:
    """Read the driver's declared targets, reporting a blocking invocation.

    ``netsuke generate`` is a blocking subprocess. The executable can come from
    ``POST_TURN_NETSUKE_BIN``, so the record names the driver instead and keeps
    the environment-derived path out of it. A make manifest is parsed without
    running a command, so it produces no command record.
    """
    observer = stage.observer
    token: CommandToken | None = None
    if observer is not None and stage.driver.name == "netsuke":
        token = observer.command_started("targets", f"{stage.driver.name} generate", [])
    available = stage.enumerate_targets(stage.repo, stage.driver)
    if observer is not None and token is not None:
        observer.command_finished(token, 0 if isinstance(available, set) else 1)
    return available


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
    match _enumerate_targets(stage):
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
    results: list[CommandResult] = []
    for kind, targets in groups:
        if not targets:
            continue
        request = BuildTargetRequest(stage.driver, kind, list(targets))
        results.append(_run_build_group(stage, request))
    return tuple(results)


def _run_build_group(stage: QualityStage, request: BuildTargetRequest) -> CommandResult:
    """Run one target group, reporting its start and exit status.

    The executable can come from ``POST_TURN_MAKE_BIN`` or
    ``POST_TURN_NETSUKE_BIN``, so the record names the driver in its place and
    keeps the environment-derived path out of the durable log.
    """
    observer = stage.observer
    token: CommandToken | None = None
    if observer is not None:
        # The executable can come from ``POST_TURN_MAKE_BIN`` or
        # ``POST_TURN_NETSUKE_BIN``, so the record names the driver in its
        # place and keeps the environment-derived path out of the durable log.
        command = f"{request.driver.name} {' '.join(request.targets)}"
        token = observer.command_started(request.kind, command, request.targets)
    result = stage.run_commands(stage.repo, request, stage.max_out)
    if observer is not None:
        observer.command_finished(token, int(result.exit_code))
    return result


def _targets_run(
    targets: TargetSelection, commands: tuple[CommandResult, ...]
) -> list[str]:
    """Return the present targets whose group actually started its executable.

    A group that never started contributes no command, so its targets are not
    reported as run. ``kind`` names the group, which is how a result is mapped
    back to the targets it executed. A result whose executable did not launch
    is excluded as well: ``run_build_targets`` reports one with exit code 127
    when the executable is missing, which would otherwise record targets that
    never ran. A child that started and returned 127 itself stays included,
    because its ``launched`` flag is true.
    """
    ran = {command.kind for command in commands if command.launched}
    executed: list[str] = []
    if "code" in ran:
        executed.extend(targets.code_targets)
    if "markdown" in ran:
        executed.extend(targets.markdown_targets)
    return executed


def _observation_fields(
    stage: QualityStage,
    selection: CategorySelection,
    targets: TargetSelection | None,
    commands: tuple[CommandResult, ...],
) -> dict[str, object]:
    """Return the bounded quality facts a record keeps for one evaluation.

    The fields are the settings and target names the run selected. No command
    output, path, or environment value reaches them. Before enumeration
    succeeds there is no present-target list, so the run list is empty rather
    than claiming targets the run never selected.
    """
    fields: dict[str, object] = {
        "build_driver": stage.driver.name,
        "targets_requested": list(selection.targets_requested),
        # The full enumeration, so a declared target the categories did not
        # select is still reported as available.
        "targets_available": (
            [] if targets is None else list(targets.targets_available)
        ),
        "targets_skipped": ([] if targets is None else list(targets.targets_skipped)),
        "targets_run": [] if targets is None else _targets_run(targets, commands),
    }
    return fields


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


def _observe_quality(
    stage: QualityStage,
    selection: CategorySelection,
    targets: TargetSelection | None,
    commands: tuple[CommandResult, ...],
) -> None:
    """Publish the selection facts once the evaluation is settled."""
    if stage.observer is None:
        return
    stage.observer.quality_selected(
        _observation_fields(stage, selection, targets, commands)
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
