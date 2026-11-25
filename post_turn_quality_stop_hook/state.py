"""Shared data structures for the stop hook.

Defines the hook reporting snapshot (HookState), immutable stage results
used by quality evaluation, branch-state gate decisions
(BranchStateGateDecision), preparation results (PreparationReady,
PreparationStop, PreparationBlock), runtime options (StopCheckOptions), and
shared constants.
"""

from __future__ import annotations

import dataclasses
import typing as typ

from post_turn_quality_stop_hook.config import Config

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.errors import HookError
    from post_turn_quality_stop_hook.git_facts import GitFacts
    from post_turn_quality_stop_hook.quality_policy import CommandResult

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


@dataclasses.dataclass(slots=True, frozen=True)
class HookState:
    """Reporting snapshot assembled by the stop hook.

    The pipeline builds a new snapshot from immutable stage results rather
    than mutating a shared state object. Collection fields are tuples or
    frozensets, so neither the snapshot nor its recorded results can be
    changed after assembly.

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
    changed_files: tuple[str, ...] = ()
    categories: frozenset[str] = frozenset()
    build_driver: str | None = None
    targets_requested: tuple[str, ...] = ()
    targets_present: tuple[str, ...] = ()
    targets_run: tuple[str, ...] = ()
    targets_skipped: tuple[str, ...] = ()
    commands: tuple[CommandResult, ...] = ()
    fetched: bool = False
    error: str | None = None
    git_facts: GitFacts | None = None


@dataclasses.dataclass(slots=True, frozen=True)
class BranchStateGateDecision:
    """Decision returned by a branch-state gate.

    This is the value the gate policy returns and the observation seam records;
    it carries no output of its own, so a gate can describe what it decided
    without printing or logging.

    Attributes
    ----------
    gate
        Stable gate name for logs and tests.
    outcome
        One of ``pass``, ``skip``, or ``block``.
    payload
        JSON payload to emit when the gate blocks.
    matched_branch
        Protected branch that caused a skip, when applicable.
    match_kind
        Whether the protected match came from the local or upstream branch.
    error
        Non-blocking hook error observed while evaluating the gate.
    reason
        Short, output-free explanation recorded in the decision log.
    warning
        Fixed, output-free text the boundary emits as standard telemetry.
        Gates describe what the operator should see; the boundary writes it.

    """

    gate: str
    outcome: typ.Literal["pass", "skip", "block"]
    payload: dict[str, str] | None = None
    matched_branch: str | None = None
    match_kind: typ.Literal["local", "upstream"] | None = None
    error: HookError | None = None
    reason: str | None = None
    warning: str | None = None

    @property
    def should_block(self) -> bool:
        """Whether this decision should stop later gate evaluation."""
        return self.outcome == "block"


@dataclasses.dataclass(slots=True, frozen=True)
class PreparationReady:
    """Preparation succeeded and quality checks may proceed.

    Attributes
    ----------
    state
        Reporting snapshot assembled while preparing.
    repo
        Resolved repository root.

    """

    state: HookState
    repo: Path


@dataclasses.dataclass(slots=True, frozen=True)
class PreparationStop:
    """Preparation found a non-repository directory and must exit quietly.

    Attributes
    ----------
    state
        Reporting snapshot assembled before the stop.

    """

    state: HookState


@dataclasses.dataclass(slots=True, frozen=True)
class PreparationBlock:
    """Preparation failed and must be reported as a blocking response.

    Attributes
    ----------
    state
        Reporting snapshot carrying the failure.
    error
        Failure that blocked preparation.
    repo
        Repository root discovered before the failure, when available.
    reason
        Fixed, output-free explanation recorded in the decision log. The
        failure's message can carry command output, so it is rendered for the
        user but never recorded.

    """

    state: HookState
    error: HookError
    repo: Path | None = None
    reason: str | None = None


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

    categories: frozenset[str] = frozenset()
    targets_requested: tuple[str, ...] = ()


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
    targets_available
        Every target the driver declared, in sorted order. This is the full
        enumeration, not the requested subset: a driver may declare targets
        that the detected categories never select, and a record that reuses
        ``targets_present`` would report selected availability instead. The
        field is kept apart from ``targets_present``, which stays the list the
        run selects from and executes.

    """

    targets_requested: tuple[str, ...] = ()
    targets_present: tuple[str, ...] = ()
    targets_skipped: tuple[str, ...] = ()
    code_targets: tuple[str, ...] = ()
    markdown_targets: tuple[str, ...] = ()
    targets_available: tuple[str, ...] = ()


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

    categories: frozenset[str] = frozenset()
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
        return any(command.exit_code != 0 for command in self.commands)


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
