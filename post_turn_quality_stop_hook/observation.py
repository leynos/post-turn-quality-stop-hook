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
    from post_turn_quality_stop_hook.errors import HookError
    from post_turn_quality_stop_hook.observer import PipelineObserver
    from post_turn_quality_stop_hook.state import BranchStateGateDecision

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


def _error_text(error: HookError | None) -> str | None:
    """Return a failure's text for the observer's bounded error category.

    The recorder reduces any failure to a fixed category, so the command output
    a Git error may carry never reaches the durable record.
    """
    return None if error is None else str(error)
