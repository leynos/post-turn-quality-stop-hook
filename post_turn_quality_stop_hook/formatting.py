"""Hook state formatting and failure reporting.

Formats blocking reasons for failed quality checks and emits structured JSON
output for the Claude Code hook contract. Category detection is policy and
lives in `quality_policy`.
"""

from __future__ import annotations

import json
import typing as typ

from post_turn_quality_stop_hook.disk_space import LowSpace, format_bytes
from post_turn_quality_stop_hook.quality_policy import (
    MAX_CHANGED_FILES_IN_REASON,
    CommandResult,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from post_turn_quality_stop_hook.state import HookState


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


def low_space_notice(low: cabc.Iterable[LowSpace]) -> str:
    """Render the non-blocking notice for volumes below the free-space floor.

    Parameters
    ----------
    low
        Volumes measured below the floor, in probe order.

    Returns
    -------
    str
        One-line operator notice.

    """
    volumes = ", ".join(
        f"{space.path} has {format_bytes(space.free_bytes)} free" for space in low
    )
    return f"quality gates skipped: {volumes}; free disk space and re-run"


def disk_exhaustion_notice(repo: Path, groups: cabc.Iterable[str]) -> str:
    """Render the non-blocking notice for an indeterminate quality run.

    Parameters
    ----------
    repo
        Repository whose quality gates could not decide.
    groups
        Target groups whose commands failed with disk exhaustion.

    Returns
    -------
    str
        One-line operator notice naming the affected groups.

    """
    affected = ", ".join(dict.fromkeys(groups))
    cause = (
        f"while running {affected} targets"
        if affected
        else "while reading build targets"
    )
    return (
        f"quality gates skipped: {repo} reported disk exhaustion {cause} "
        "(No space left on device); free disk space and re-run"
    )


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
    payload = {"decision": "block", "reason": format_reason(state)}
    print(json.dumps(payload))
    return 0
