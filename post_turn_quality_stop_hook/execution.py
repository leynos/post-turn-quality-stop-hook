"""Build execution, target selection, and output capture.

Runs build targets, captures and truncates output, maps changed
file categories to build targets, and provides the structured
result types used across the hook.
"""

from __future__ import annotations

import dataclasses
import typing as typ

from post_turn_quality_stop_hook.git import run

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.driver import BuildDriver

CATS_TO_TARGETS: dict[str, list[str]] = {
    "python_ts": ["check-fmt", "lint", "typecheck"],
    "rust": ["check-fmt", "lint"],
    "markdown": ["markdownlint"],
}

CODE_CATS = {"python_ts", "rust"}

MD_CATS = {"markdown"}

MAX_CHANGED_FILES_IN_REASON = 60

BLOCKED_STATUS = 1


class CommandResult(typ.TypedDict):
    """Result of a build-target invocation.

    Attributes
    ----------
    kind
        Label describing the target group (``"code"`` or ``"markdown"``).
    cmd
        Full command that was executed.
    exit_code
        Process exit code.
    stdout
        Captured standard output (may be truncated).
    stderr
        Captured standard error.

    """

    kind: str
    cmd: str
    exit_code: int
    stdout: str
    stderr: str


@dataclasses.dataclass(slots=True, frozen=True)
class BuildTargetRequest:
    """A grouped build-target invocation.

    Attributes
    ----------
    driver
        Build driver used to run the targets.
    kind
        Label describing the target group.
    targets
        Build targets to run.

    """

    driver: BuildDriver
    kind: str
    targets: list[str]


def truncate(text: str, max_chars: int) -> str:
    """Truncate text to a maximum length.

    Parameters
    ----------
    text
        Text to truncate.
    max_chars
        Maximum number of characters to keep.

    Returns
    -------
    str
        Truncated text with a placeholder if needed.

    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "\n... (output truncated) ...\n"
    if max_chars <= len(marker):
        return text[:max_chars]
    remaining = max_chars - len(marker)
    head = remaining // 2
    tail = remaining - head
    return text[:head] + marker + text[-tail:]


def dedup_preserve_order(items: list[str]) -> list[str]:
    """Deduplicate items while preserving order.

    Parameters
    ----------
    items
        Items to deduplicate.

    Returns
    -------
    list[str]
        Deduplicated items in original order.

    """
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def run_build_targets(
    repo: Path,
    request: BuildTargetRequest,
    max_out: int,
) -> CommandResult:
    """Run build targets and capture output.

    Parameters
    ----------
    repo
        Repository root path.
    request
        Grouped build-target request.
    max_out
        Maximum number of output characters to capture.

    Returns
    -------
    CommandResult
        TypedDict with keys ``kind``, ``cmd``, ``exit_code``, ``stdout``,
        and ``stderr`` representing execution metadata and captured output.

    """
    if not request.targets:
        return {
            "kind": request.kind,
            "cmd": "",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }

    cmd = build_command(request.driver, request.targets)
    try:
        p = run(cmd, repo)
    except FileNotFoundError as exc:
        return {
            "kind": request.kind,
            "cmd": " ".join(cmd),
            "exit_code": 127,
            "stdout": "",
            "stderr": f"{request.driver.executable} not found on PATH: {exc}",
        }
    return {
        "kind": request.kind,
        "cmd": " ".join(cmd),
        "exit_code": int(p.returncode),
        "stdout": truncate(p.stdout, max_out),
        "stderr": truncate(p.stderr, max_out),
    }


def build_command(driver: BuildDriver, targets: list[str]) -> list[str]:
    """Return the command used to build targets with a driver."""
    if driver.name == "netsuke":
        return [driver.executable, "build", *targets]
    return [driver.executable, "--no-print-directory", *targets]


def targets_for_categories(
    categories: dict[str, bool],
    *,
    include: set[str] | None = None,
) -> list[str]:
    """Expand enabled categories into build targets.

    Parameters
    ----------
    categories
        Mapping of category flags.
    include
        Optional subset of categories to include.

    Returns
    -------
    list[str]
        Deduplicated target list.

    """
    requested: list[str] = []
    for category, enabled in categories.items():
        if not enabled:
            continue
        if include is not None and category not in include:
            continue
        requested.extend(CATS_TO_TARGETS.get(category, []))
    return dedup_preserve_order(requested)
