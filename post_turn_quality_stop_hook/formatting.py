"""Hook state formatting and failure reporting.

Detects change categories from file lists, formats blocking
reasons for failed quality checks, and emits structured JSON
output for the Claude Code hook contract.
"""

from __future__ import annotations

import json
from pathlib import Path

from post_turn_quality_stop_hook.execution import (
    MAX_CHANGED_FILES_IN_REASON,
    CommandResult,
)
from post_turn_quality_stop_hook.state import (
    MD_EXTS,
    PY_TS_EXTS,
    RUST_EXTS,
    HookState,
    default_categories,
)


def detect_categories(files: list[str]) -> dict[str, bool]:
    """Detect change categories from a file list.

    Parameters
    ----------
    files
        List of file paths.

    Returns
    -------
    dict[str, bool]
        Mapping of category names to detection flags.

    """
    cats = default_categories()
    for f in files:
        ext = Path(f).suffix.lower()
        if ext in PY_TS_EXTS:
            cats["python_ts"] = True
        if ext in RUST_EXTS:
            cats["rust"] = True
        if ext in MD_EXTS:
            cats["markdown"] = True
    return cats


def _detected_category_labels(categories: dict[str, bool]) -> list[str]:
    """Return human-readable labels for detected change categories."""
    labels: list[str] = []
    if categories.get("python_ts"):
        labels.append("Python/TypeScript")
    if categories.get("rust"):
        labels.append("Rust")
    if categories.get("markdown"):
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
    if state.targets_run:
        lines.append("Targets run: " + " ".join(state.targets_run))
    if state.targets_skipped:
        lines.append("Targets skipped (missing): " + " ".join(state.targets_skipped))
    return lines


def _format_command_failure(command: CommandResult) -> list[str]:
    """Format one failed command for the blocking reason."""
    cmd = command.get("cmd", "")
    code = command.get("exit_code", "?")
    combined = "\n".join([
        x for x in [command.get("stdout", ""), command.get("stderr", "")] if x
    ]).strip()
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

    failures = [c for c in state.commands if int(c.get("exit_code", 0)) != 0]
    for command in failures:
        lines.extend(_format_command_failure(command))

    lines.extend((
        "",
        "Fix the failures above. The checks will re-run at the end of the next turn.",
    ))
    return "\n".join(lines)


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


def fail_state(state: HookState, message: str | None) -> int:
    """Mark the state as failed and emit a block response.

    Parameters
    ----------
    state
        Hook execution state.
    message
        Error message to include in the response.

    Returns
    -------
    int
        Exit code for the hook.

    """
    state.ok = False
    state.error = message
    return block_and_print(state)
