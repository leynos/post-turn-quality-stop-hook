"""Report what the pipeline decided through the run's observer.

Gate functions decide; this module reports. Every gate outcome, quality
selection, and fixed operator warning the pipeline emits is routed through one
place, so adding, removing, or reordering a gate cannot change how or when an
event is recorded.

The module depends only on :class:`~post_turn_quality_stop_hook.observer.\
PipelineObserver` and the stop hook's domain types. It holds no policy of its
own: a caller supplies the decision or the fixed text, and the helpers decide
only which channel carries it. :class:`PipelineObserver` is the pipeline's
single telemetry channel; nothing here writes the standard-library logger, so a
test that substitutes an observer sees every event the run reports.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import contextlib

    from post_turn_quality_stop_hook.errors import HookError
    from post_turn_quality_stop_hook.observer import CommandToken, PipelineObserver
    from post_turn_quality_stop_hook.quality_policy import CommandResult
    from post_turn_quality_stop_hook.state import (
        BranchStateGateDecision,
        CategorySelection,
        TargetSelection,
    )

#: Event family for branch-state gate telemetry. The record carries the gate
#: name and outcome that explain a skip or a block.
_BRANCH_STATE_EVENT = "branch_state_gate_{outcome}"


def record_gate_decision(
    observer: PipelineObserver, decision: BranchStateGateDecision
) -> None:
    """Record one gate decision and report its bounded telemetry.

    This is the only place the pipeline writes a gate outcome, so adding,
    removing, or reordering a gate cannot change how or when it is recorded.
    Only a skip or a block is reported as telemetry: a pass needs no operator
    attention, and reporting it would make the telemetry a second decision
    record.
    """
    observer.gate_decided(
        decision.gate, decision.outcome, decision.reason, _error_text(decision.error)
    )
    if decision.outcome in {"skip", "block"}:
        level = "block" if decision.should_block else "skip"
        observer.gate_telemetry(
            _BRANCH_STATE_EVENT.format(outcome=level),
            {
                "gate": decision.gate,
                "outcome": decision.outcome,
                "matched_branch": decision.matched_branch,
                "match_kind": decision.match_kind,
            },
        )
    if decision.warning is not None:
        observer.warning(decision.warning)
    if decision.should_block:
        # These gates return their notice rather than rendering one, so the
        # decision is marked here and emitted by the caller.
        observer.blocked(decision.reason or decision.gate)


def record_quality_decision(
    observer: PipelineObserver,
    gate: str,
    outcome: typ.Literal["pass", "skip", "block", "error"],
    reason: str | None,
) -> None:
    """Record the quality gate under quality-specific event names.

    Quality is not a branch-state gate, so its telemetry is named
    ``quality_gate_*``; the branch-state gates keep their own
    ``branch_state_gate_*`` names. Both a skip and a block remain visible.
    """
    observer.gate_decided(gate, outcome, reason, None)
    if outcome in {"skip", "block"}:
        observer.gate_telemetry(
            f"quality_gate_{outcome}",
            {"gate": gate, "outcome": outcome},
        )
    if outcome == "block":
        observer.blocked(reason or gate)


def warn_build_targets_unavailable(
    observer: PipelineObserver, error: HookError
) -> None:
    """Report that build targets could not be enumerated.

    The diagnostic is operator-facing rather than durable telemetry, so it goes
    through the observer's warning channel. A no-op observer still writes it to
    standard error, so disabling the log never hides the warning.
    """
    observer.warning(f"Could not enumerate build targets: {error}")


def start_command(
    observer: PipelineObserver, kind: str, command: str, targets: list[str]
) -> CommandToken | None:
    """Report that a blocking command is about to start.

    The executable can come from the environment, so a caller passes the
    driver's name in its place and keeps the environment-derived path out of
    the durable record.
    """
    return observer.command_started(kind, command, targets)


def finish_command(
    observer: PipelineObserver, token: CommandToken | None, exit_code: int
) -> None:
    """Report the exit status a command actually returned."""
    observer.command_finished(token, exit_code)


def record_stage_start(
    observer: PipelineObserver, name: str
) -> contextlib.AbstractContextManager[None]:
    """Return the context manager that records one stage's start and duration."""
    return observer.stage(name)


def mark_blocked(observer: PipelineObserver, reason: str | None) -> None:
    """Record that the invocation emitted a blocking decision."""
    observer.blocked(reason)


def publish_quality_selection(
    observer: PipelineObserver, fields: cabc.Mapping[str, object]
) -> None:
    """Publish the bounded quality facts a record keeps for one evaluation."""
    observer.quality_selected(fields)


def quality_targets_run(
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


def quality_observation_fields(
    *,
    build_driver: str,
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
    return {
        "build_driver": build_driver,
        "targets_requested": list(selection.targets_requested),
        # The full enumeration, so a declared target the categories did not
        # select is still reported as available.
        "targets_available": (
            [] if targets is None else list(targets.targets_available)
        ),
        "targets_skipped": ([] if targets is None else list(targets.targets_skipped)),
        "targets_run": (
            [] if targets is None else quality_targets_run(targets, commands)
        ),
    }


def _error_text(error: HookError | None) -> str | None:
    """Return a failure's text for the observer's bounded error category.

    The recorder reduces any failure to a fixed category, so the command output
    a Git error may carry never reaches the durable record.
    """
    return None if error is None else str(error)
