"""Domain policy for quality targets and category detection.

This module owns the pure rules that map changed files to categories and
categories to build targets, together with the value types those rules and the
quality stage exchange. It imports no process, subprocess, or formatting
module, so policy can be exercised without an adapter or a build tool.
"""

from __future__ import annotations

import dataclasses
import typing as typ
from pathlib import Path

from post_turn_quality_stop_hook.state import CODE_EXTS, MARKDOWN_EXTS

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from post_turn_quality_stop_hook.driver import BuildDriver

TARGETS_BY_CATEGORY: dict[str, list[str]] = {
    "code": ["check-fmt", "lint", "typecheck"],
    "markdown": ["markdownlint", "nixie"],
}

CODE_CATS = {"code"}

MD_CATS = {"markdown"}

MAX_CHANGED_FILES_IN_REASON = 60


@dataclasses.dataclass(slots=True, frozen=True)
class CommandResult:
    """Immutable result of a build-target invocation.

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
    launched
        Whether the executable actually started. ``False`` marks a result
        reported for a command that never ran, so a caller never has to infer
        a launch failure from ``exit_code``: a child that did start and exit
        with ``127`` is still ``True``.

    """

    kind: str
    cmd: str
    exit_code: int
    stdout: str
    stderr: str
    launched: bool = True


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


def dedup_preserve_order(items: list[str]) -> list[str]:
    """Deduplicate items while preserving order.

    Parameters
    ----------
    items
        Input items.

    Returns
    -------
    list[str]
        Deduplicated items in first-seen order.

    """
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def detect_categories(files: cabc.Iterable[str]) -> set[str]:
    """Detect change categories from a file list.

    Parameters
    ----------
    files
        List of file paths.

    Returns
    -------
    set[str]
        Detected category names.

    """
    cats: set[str] = set()
    for f in files:
        ext = Path(f).suffix.lower()
        if ext in CODE_EXTS:
            cats.add("code")
        if ext in MARKDOWN_EXTS:
            cats.add("markdown")
    return cats


def targets_for_categories(
    categories: cabc.Set[str],
    *,
    include: cabc.Set[str] | None = None,
) -> list[str]:
    """Expand enabled categories into build targets.

    Parameters
    ----------
    categories
        Detected category names.
    include
        Optional subset of categories to include.

    Returns
    -------
    list[str]
        Deduplicated target list.

    """
    requested: list[str] = []
    for category in ("code", "markdown"):
        if category not in categories:
            continue
        if include is not None and category not in include:
            continue
        requested.extend(TARGETS_BY_CATEGORY[category])
    return dedup_preserve_order(requested)


def select_targets(
    categories: cabc.Set[str], available_targets: cabc.Set[str]
) -> list[str]:
    """Return existing targets selected by detected categories."""
    requested = targets_for_categories(categories)
    return [target for target in requested if target in available_targets]
