"""Run the build-target groups one quality selection resolved.

This unit answers *what ran*: it invokes the stage's command runner once per
non-empty group, code before Markdown, and reports each blocking command
through :mod:`post_turn_quality_stop_hook.observation` with the status the
runner returned. Selection and enumeration live in
:mod:`post_turn_quality_stop_hook.quality_targets`, so a selection can be
exercised without starting a build tool.
"""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook import observation
from post_turn_quality_stop_hook.process import LAUNCH_FAILURE_STATUS
from post_turn_quality_stop_hook.quality_policy import BuildTargetRequest

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook.quality_policy import CommandResult
    from post_turn_quality_stop_hook.quality_stage import QualityStage
    from post_turn_quality_stop_hook.state import TargetSelection


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
        result = _run_build_group(stage, request)
        results.append(result)
        if result.launched:
            # Publish before the next group starts. A signal that interrupts
            # that group still leaves this group's targets in the record.
            _publish_run_targets(stage, selection, tuple(results))
    return tuple(results)


def _publish_run_targets(
    stage: QualityStage,
    selection: TargetSelection,
    commands: tuple[CommandResult, ...],
) -> None:
    """Publish the targets whose groups have started so far.

    ``quality_targets_run`` excludes a group whose executable never started,
    so a launch failure contributes no target and the distinction between "did
    not run" and "ran and failed" is preserved. The final full selection is
    published by ``evaluate_changes`` once both groups return.
    """
    if stage.observer is None:
        return
    observation.publish_quality_selection(
        stage.observer,
        observation.quality_observation_fields(
            build_driver=stage.driver.name,
            build_driver_reason=stage.driver.selection_reason,
            targets_requested=selection.targets_requested,
            targets=selection,
            commands=commands,
        ),
    )


def _run_build_group(stage: QualityStage, request: BuildTargetRequest) -> CommandResult:
    """Run one target group, reporting its start and exit status.

    The executable can come from ``POST_TURN_MAKE_BIN`` or
    ``POST_TURN_NETSUKE_BIN``, so the record names the driver in its place and
    keeps the environment-derived path out of the durable log.

    The finish record carries :func:`_recorded_status`, so a group whose
    executable never started is distinguishable from one that started and
    exited with the same number.
    """
    observer = stage.observer
    token = None
    if observer is not None:
        command = f"{request.driver.name} {' '.join(request.targets)}"
        token = observation.start_command(
            observer, request.kind, command, request.targets
        )
    result = stage.run_commands(stage.repo, request, stage.max_out)
    if observer is not None:
        observation.finish_command(observer, token, _recorded_status(result))
    return result


def _recorded_status(result: CommandResult) -> int:
    """Status a command record should carry for one build-group result.

    A group whose executable never started has no child status. Recording its
    synthetic status would make it indistinguishable from a child that started
    and exited with that number, so the shared launch-failure value is
    recorded instead, exactly as ``TargetEnumeration.recorded_exit_status``
    does for the enumerating command.
    """
    return int(result.exit_code) if result.launched else LAUNCH_FAILURE_STATUS
