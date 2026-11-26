"""The collaborators one quality evaluation is assembled from.

A quality evaluation reads a build driver's declared targets and then runs the
selected groups. Both steps are blocking processes, so each is a port the
caller supplies: :func:`post_turn_quality_stop_hook.quality_targets.\
enumerate_quality_targets` reads through ``enumerate_targets`` and
:func:`post_turn_quality_stop_hook.quality_execution.execute_quality_targets`
runs through ``run_commands``. A unit test can therefore decide a gate from
prepared answers without a subprocess.

Reporting is a keyword-only option rather than a hidden default: ``observer``
is ``None`` for a caller that does not observe, so no test needs a reporting
seam it never inspects, and the orchestrator supplies the run's observer, which
is a no-op when the decision log is disabled.
"""

from __future__ import annotations

import dataclasses
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from post_turn_quality_stop_hook.driver import BuildDriver, TargetEnumeration
    from post_turn_quality_stop_hook.observer import PipelineObserver
    from post_turn_quality_stop_hook.quality_policy import (
        BuildTargetRequest,
        CommandResult,
    )

    #: Reads the driver's declared targets. For Netsuke it runs ``netsuke
    #: generate`` and carries that child's exit status; a Makefile read runs
    #: no command.
    EnumerateTargets = cabc.Callable[[Path, BuildDriver], TargetEnumeration]

    #: Runs one grouped target request and returns its captured result.
    RunCommands = cabc.Callable[[Path, BuildTargetRequest, int], CommandResult]


@dataclasses.dataclass(slots=True, frozen=True)
class QualityStage:
    """Adapters and limits needed to evaluate one repository state.

    Attributes
    ----------
    repo
        Repository root path.
    driver
        Build driver selected for quality gates.
    max_out
        Maximum number of output characters to capture per command.
    enumerate_targets
        Port that reads the driver's declared targets. For Netsuke it runs
        ``netsuke generate``; the read failure is reported as ``HookError``.
    run_commands
        Port that runs one grouped target request and returns its result.
    observer
        Receives the commands the stage runs and the selection it makes.
        ``None`` records nothing, so a unit test needs no observation seam.

    """

    repo: Path
    driver: BuildDriver
    max_out: int
    enumerate_targets: EnumerateTargets
    run_commands: RunCommands
    observer: PipelineObserver | None = None
