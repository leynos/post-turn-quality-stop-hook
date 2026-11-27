"""Claude Code stop-hook quality gate.

Runs deterministic checks — formatting, linting, type-checking, and
Markdown linting — against files changed relative to ``origin/main``.
When any check fails, the hook blocks the turn and emits a structured
JSON report describing the failures so the agent can correct them.

Public API
----------
``main``
    The entry point re-exported from `hook`. This function reads the
    hook input from stdin, determines the working directory, and invokes
    the full quality-gate pipeline. It is mapped to the
    ``post-turn-quality-stop-hook`` console-script entry point defined
    in ``pyproject.toml``.

Implementation
--------------
This module is a thin re-export layer. ``hook.py`` contains the CLI
entry point (argument parsing, main). Implementation is split across:

- ``git.py`` — read-only Git queries and configuration reads.
- ``git_location_paths.py`` — the path and repository-location rules the
  decision log asks before writing or deleting a file.
- ``git_location_query.py`` — the Git adapter behind those rules, answering
  from one environment snapshot and one injected runner.
- ``git_location.py`` — the tri-state ownership answers those two compose.
- ``process.py`` — bounded subprocess execution and process-group cleanup.
- ``redaction.py`` — credential removal from remote URLs.
- ``fetch_port.py`` — the fetch-observer ports a Git fetch reports through.
- ``pull_request.py`` — the domain-facing pull-request summary the rebase gate
  decides from.
- ``upstream_port.py`` — domain-facing upstream comparison types.
- ``upstream_label.py`` — the display rule for a prepared upstream comparison.
- ``upstream.py`` — upstream-head fetch, temporary-ref lifecycle, and the Git
  implementation of the upstream port.
- ``branch_state_port.py`` — the branch-state, pull-request lookup and
  build-target ports the branch gates decide from.
- ``branch_state_git.py`` — the Git implementation of the branch-state port.
- ``repository_port.py`` — the root-bound repository questions preparation and
  the uncommitted gate ask, free of paths and storage locations.
- ``repository_git.py`` — the Git implementation of the repository-operations
  port, bound to a resolved repository root.
- ``driver.py`` — build-driver discovery and manifest parsing.
- ``execution.py`` — build execution, target selection, output capture.
- ``quality.py`` — the quality stage's composition and evaluation snapshot.
- ``quality_stage.py`` — the ports and limits one evaluation is built from.
- ``quality_targets.py`` — change-category detection and target selection.
- ``quality_execution.py`` — build-target group execution.
- ``observer.py`` — the ``PipelineObserver`` every unit reports through.
- ``observation.py`` — the reporting helpers that speak that observer protocol.
- ``repository_facts.py`` — log-only repository facts observed for the record,
  through the ``RepositoryFactsSource`` port that ``repository_facts_git.py``
  implements with the Git plumbing.
- ``branch_gates.py`` — branch-state and pull-request rebase decisions, made
  from injected ports and without rendering.
- ``fetch_observation.py`` — the fetch-observer adapter for one real fetch.
- ``decision_log/`` — the durable per-invocation decision log.
- ``state.py`` — shared data structures, stage results, and runtime options.

Exposing only ``main`` here keeps the package entry point clean while
allowing consumers to import internals directly from the submodules
when needed.

The re-export is lazy. Importing ``hook`` pulls in the whole pipeline, the Git
adapters, and the decision-log storage layer, so an eager ``from .hook import
main`` would load every adapter for a caller that only wanted a domain module
such as ``post_turn_quality_stop_hook.decision_log.log``. PEP 562 module
``__getattr__`` keeps ``from post_turn_quality_stop_hook import main`` working
while leaving the import graph of a submodule to that submodule.
"""

from __future__ import annotations

import typing as typ

__all__ = ["main"]

if typ.TYPE_CHECKING:
    from .hook import main as main


def __getattr__(name: str) -> typ.Any:  # ruff:ignore[any-type]  # module __getattr__ returns the re-exported object.
    """Resolve the public ``main`` re-export on first use."""
    if name == "main":
        from .hook import main as hook_main

        return hook_main
    message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(message)
