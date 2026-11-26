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
        results.append(_run_build_group(stage, request))
    return tuple(results)


def _run_build_group(stage: QualityStage, request: BuildTargetRequest) -> CommandResult:
    """Run one target group, reporting its start and exit status.

    The executable can come from ``POST_TURN_MAKE_BIN`` or
    ``POST_TURN_NETSUKE_BIN``, so the record names the driver in its place and
    keeps the environment-derived path out of the durable log.
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
        observation.finish_command(observer, token, int(result.exit_code))
    return result
