"""Hook state formatting and failure reporting.

Formats blocking reasons for failed quality checks and emits structured JSON
output for the Claude Code hook contract. Category detection is policy and
lives in `quality_policy`.
"""

from __future__ import annotations

import json
import typing as typ

from post_turn_quality_stop_hook.quality_policy import (
    MAX_CHANGED_FILES_IN_REASON,
    CommandResult,
)
from post_turn_quality_stop_hook.templates import render
from post_turn_quality_stop_hook.upstream_label import upstream_label

# ``upstream_label`` is re-exported here because this module renders the
# unpushed payload that names the upstream; the rule itself lives in its own
# domain-facing module so the gate can use it without importing the formatter.

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from post_turn_quality_stop_hook.state import BranchStateGateDecision, HookState
    from post_turn_quality_stop_hook.upstream_port import UpstreamComparison


def _detected_category_labels(categories: cabc.Set[str]) -> list[str]:
    """Return human-readable labels for detected change categories."""
    labels: list[str] = []
    if "code" in categories:
        labels.append("Code")
    if "markdown" in categories:
        labels.append("Markdown")
    return labels


def _format_changed_files(state: HookState) -> list[str]:
    """Format the changed-file section for a blocking reason."""
    base_ref = state.base_ref or "?"
    changed = state.changed_files
    lines = ["", f"Changed files vs {base_ref}: {len(changed)}"]
    lines.extend(f"- {path}" for path in changed[:MAX_CHANGED_FILES_IN_REASON])
    if len(changed) > MAX_CHANGED_FILES_IN_REASON:
        remaining = len(changed) - MAX_CHANGED_FILES_IN_REASON
        lines.append(f"- ... (+{remaining} more)")
    return lines


def _format_target_summary(state: HookState) -> list[str]:
    """Format requested, run, and skipped build targets."""
    lines: list[str] = []
    if state.build_driver:
        lines.extend(("", f"Build driver: {state.build_driver}"))
    if state.targets_requested:
        lines.extend((
            "",
            "Requested build targets: " + " ".join(state.targets_requested),
        ))
    if state.targets_present:
        lines.append("Targets present: " + " ".join(state.targets_present))
    if state.targets_run:
        lines.append("Targets run: " + " ".join(state.targets_run))
    if state.targets_skipped:
        lines.append("Targets skipped (missing): " + " ".join(state.targets_skipped))
    return lines


def _format_command_failure(command: CommandResult) -> list[str]:
    """Format one failed command for the blocking reason."""
    cmd = command.cmd
    code = command.exit_code
    combined = "\n".join([x for x in [command.stdout, command.stderr] if x]).strip()
    return [
        "",
        f"Command failed (exit {code}): {cmd}",
        "```",
        combined or "(no output captured)",
        "```",
    ]


def format_reason(state: HookState) -> str:
    """Format a blocking reason for hook output.

    Parameters
    ----------
    state
        Hook execution state.

    Returns
    -------
    str
        Human-readable reason string.

    """
    lines: list[str] = ["Post-turn checks failed."]

    if state.error:
        lines.extend(("", f"Error: {state.error}"))

    base_ref = state.base_ref or "?"
    base_commit = state.base_commit or "?"
    lines.extend(("", f"Diff base: {base_ref} ({base_commit})"))
    lines.extend(_format_changed_files(state))

    detected = _detected_category_labels(state.categories)
    if detected:
        lines.extend(("", "Detected change types: " + ", ".join(detected)))

    lines.extend(_format_target_summary(state))

    failures = [command for command in state.commands if command.exit_code != 0]
    for command in failures:
        lines.extend(_format_command_failure(command))

    lines.extend((
        "",
        "Fix the failures above. The checks will re-run at the end of the next turn.",
    ))
    return "\n".join(lines)


def _emit(payload: dict[str, str], *, flush: bool = False) -> None:
    """Write one hook-contract payload to standard output.

    Every block the hook prints goes through this one emitter, so the JSON
    shape is declared in one place and an orchestration unit never assembles
    a payload itself.
    """
    print(json.dumps(payload), flush=flush)


def emit_block_payload(reason: str, *, flush: bool = False) -> None:
    """Emit a blocking payload whose reason is already rendered.

    Parameters
    ----------
    reason
        Human-readable blocking reason.
    flush
        Whether to flush standard output before returning. The early
        invalid-argument path flushes before it opens the decision-log run,
        whose Git queries must not delay the payload.

    """
    _emit({"decision": "block", "reason": reason}, flush=flush)


def block_and_print(state: HookState) -> int:
    """Emit a blocking response and return a stop code.

    Parameters
    ----------
    state
        Hook execution state.

    Returns
    -------
    int
        Exit code for the hook.

    """
    emit_block_payload(format_reason(state))
    return 0


def emit_decision_payload(decision: BranchStateGateDecision) -> None:
    """Render and emit a blocking gate payload, when the decision carries one.

    The gate decides which template and domain facts apply; rendering happens
    here, so no decision unit imports a template engine.
    """
    payload = decision.payload
    if payload is not None:
        emit_block_payload(render(payload.template, **payload.variables))


def emit_unpushed_block(comparison: UpstreamComparison) -> None:
    """Render and print the unpushed block for a blocked comparison."""
    _emit({
        "decision": "block",
        "reason": render(
            "unpushed_required.j2", upstream_ref=upstream_label(comparison)
        ),
    })
