"""Shared data structures for the stop hook.

Defines the hook reporting snapshot (HookState), immutable stage results
used by quality evaluation, preparation results
(RunStopChecksPreparation), runtime options (StopCheckOptions), and shared
constants.
"""

from __future__ import annotations

import dataclasses
import typing as typ

from post_turn_quality_stop_hook.config import Config

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.errors import HookError
    from post_turn_quality_stop_hook.execution import CommandResult
    from post_turn_quality_stop_hook.git_facts import GitFacts

CODE_EXTS = frozenset({
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".rs",
    ".go",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cxx",
    ".hh",
    ".hpp",
    ".hxx",
    ".java",
    ".kt",
    ".kts",
    ".rb",
    ".swift",
})

MARKDOWN_EXTS = frozenset({".md", ".mdx", ".markdown"})


def default_categories() -> set[str]:
    """Return a default category mapping.

    Returns
    -------
    set[str]
        Default set of detected category names.

    """
    return set()


@dataclasses.dataclass(slots=True, frozen=True)
class HookState:
    """Reporting snapshot assembled by the stop hook.

    The pipeline builds a new snapshot from immutable stage results rather
    than mutating a shared state object.

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
    categories: set[str] = dataclasses.field(default_factory=default_categories)
    build_driver: str | None = None
    targets_requested: list[str] = dataclasses.field(default_factory=list)
    targets_present: list[str] = dataclasses.field(default_factory=list)
    targets_run: list[str] = dataclasses.field(default_factory=list)
    targets_skipped: list[str] = dataclasses.field(default_factory=list)
    commands: list[CommandResult] = dataclasses.field(default_factory=list)
    fetched: bool = False
    error: str | None = None
    git_facts: GitFacts | None = None


@dataclasses.dataclass(slots=True, frozen=True)
class RunStopChecksPreparation:
    """Immutable outcome of repository preparation.

    Attributes
    ----------
    outcome
        ``ready`` to continue, ``stop`` for a quiet non-repository exit, or
        ``block`` when a mandatory preparation step failed.
    state
        Reporting snapshot assembled while preparing.
    repo
        Resolved repository root when preparation discovered one.
    error
        Failure reported when ``outcome`` is ``block``.

    """

    outcome: typ.Literal["ready", "stop", "block"]
    state: HookState
    repo: Path | None = None
    error: HookError | None = None


@dataclasses.dataclass(slots=True, frozen=True)
class CategorySelection:
    """Detected change categories and the targets they request.

    Attributes
    ----------
    categories
        Category names detected in the changed-file list.
    targets_requested
        Build targets requested by those categories.

    """

    categories: set[str] = dataclasses.field(default_factory=default_categories)
    targets_requested: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(slots=True, frozen=True)
class TargetSelection:
    """Targets available and selected for a build driver.

    Attributes
    ----------
    targets_requested
        Targets requested by the detected categories.
    targets_present
        Requested targets declared by the selected driver.
    targets_skipped
        Requested targets missing from the selected driver.
    code_targets
        Present code targets, in request order.
    markdown_targets
        Present Markdown targets, in request order.

    """

    targets_requested: tuple[str, ...] = ()
    targets_present: tuple[str, ...] = ()
    targets_skipped: tuple[str, ...] = ()
    code_targets: tuple[str, ...] = ()
    markdown_targets: tuple[str, ...] = ()


@dataclasses.dataclass(slots=True, frozen=True)
class QualityEvaluation:
    """Immutable result of the quality-check stage.

    Attributes
    ----------
    categories
        Category names detected in the changed-file list.
    build_driver
        Name of the selected build driver.
    targets_requested
        Targets requested by the detected categories.
    targets_present
        Requested targets declared by the selected driver.
    targets_skipped
        Requested targets missing from the selected driver.
    commands
        Command results captured while running the target groups.
    error
        Failure encountered before or during execution.

    """

    categories: set[str] = dataclasses.field(default_factory=default_categories)
    build_driver: str | None = None
    targets_requested: tuple[str, ...] = ()
    targets_present: tuple[str, ...] = ()
    targets_skipped: tuple[str, ...] = ()
    commands: tuple[CommandResult, ...] = ()
    error: HookError | None = None

    @property
    def blocked(self) -> bool:
        """Whether the evaluation must block the hook."""
        if self.error is not None:
            return True
        return any(command["exit_code"] != 0 for command in self.commands)


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
    config
        Merged configuration file values.
    build_driver
        Requested build driver: ``auto``, ``netsuke``, or ``make``.
    netsuke_bin
        Executable used when running Netsuke.
    make_bin
        Executable used when running Make.

    """

    always_fetch: bool
    max_out: int
    config: Config = dataclasses.field(default_factory=Config)
    compush: bool = False
    build_driver: str = "auto"
    netsuke_bin: str = "netsuke"
    make_bin: str = "make"
