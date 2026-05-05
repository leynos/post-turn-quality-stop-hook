"""Shared data structures for the stop hook.

Defines the hook execution state (HookState), preparation
results (RunStopChecksPreparation), runtime options
(StopCheckOptions), and shared constants.
"""

from __future__ import annotations

import dataclasses
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.execution import CommandResult

PY_TS_EXTS = {".py", ".pyi", ".ts", ".tsx", ".mts", ".cts"}

RUST_EXTS = {".rs"}

MD_EXTS = {".md", ".mdx", ".markdown"}


def default_categories() -> dict[str, bool]:
    """Return a default category mapping.

    Returns
    -------
    dict[str, bool]
        Default mapping of category names to enabled flags.

    """
    return {"python_ts": False, "rust": False, "markdown": False}


@dataclasses.dataclass(slots=True)
class HookState:
    """Execution state for the stop hook.

    Attributes
    ----------
    ok
        Whether the hook checks succeeded.
    base_ref
        Base ref used for comparison.
    base_commit
        Resolved merge-base commit.
    changed_files
        Files changed relative to the base commit.
    categories
        Detected change categories.
    build_driver
        Build driver selected to run quality targets.
    targets_requested
        Build targets requested based on change categories.
    targets_run
        Build targets executed.
    targets_skipped
        Requested targets that were not present for the selected driver.
    commands
        Executed commands and their outputs.
    fetched
        Whether a fetch was performed.
    error
        Error message when blocking.

    """

    ok: bool = True
    base_ref: str = "origin/main"
    base_commit: str | None = None
    changed_files: list[str] = dataclasses.field(default_factory=list)
    categories: dict[str, bool] = dataclasses.field(default_factory=default_categories)
    build_driver: str | None = None
    targets_requested: list[str] = dataclasses.field(default_factory=list)
    targets_run: list[str] = dataclasses.field(default_factory=list)
    targets_skipped: list[str] = dataclasses.field(default_factory=list)
    commands: list[CommandResult] = dataclasses.field(default_factory=list)
    fetched: bool = False
    error: str | None = None


@dataclasses.dataclass(slots=True)
class RunStopChecksPreparation:
    """Prepared state for ``run_stop_checks``.

    Attributes
    ----------
    ok
        Whether preparation succeeded and execution should continue.
    exit_code
        Exit code to return immediately when preparation did not succeed.
    state
        Hook state populated during preparation.
    repo
        Resolved repository root when preparation succeeded.

    """

    ok: bool
    exit_code: int
    state: HookState
    repo: Path | None = None


@dataclasses.dataclass(slots=True, frozen=True)
class StopCheckOptions:
    """Runtime options for stop-hook checks.

    Attributes
    ----------
    always_fetch
        Whether to always fetch origin/main.
    max_out
        Maximum number of output characters to capture.
    compush
        Whether to remind the agent to commit and push when dirty.
    build_driver
        Requested build driver: ``auto``, ``netsuke``, or ``make``.
    netsuke_bin
        Executable used when running Netsuke.
    make_bin
        Executable used when running Make.

    """

    always_fetch: bool
    max_out: int
    compush: bool = False
    build_driver: str = "auto"
    netsuke_bin: str = "netsuke"
    make_bin: str = "make"
