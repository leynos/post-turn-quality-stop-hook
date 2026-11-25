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
- ``git_location.py`` — path-location and ignore queries for the decision log.
- ``process.py`` — bounded subprocess execution and process-group cleanup.
- ``redaction.py`` — credential removal from remote URLs.
- ``upstream_port.py`` — domain-facing upstream comparison types.
- ``upstream.py`` — upstream-head fetch, temporary-ref lifecycle, and the Git
  implementation of the upstream port.
- ``driver.py`` — build-driver discovery and manifest parsing.
- ``execution.py`` — build execution, target selection, output capture.
- ``quality.py`` — quality-target enumeration and execution via injected ports.
- ``observer.py`` — the ``PipelineObserver`` every unit reports through.
- ``observation.py`` — the reporting helpers that speak that observer protocol.
- ``repository_facts.py`` — log-only repository facts observed for the record,
  through the ``RepositoryFactsSource`` port that ``repository_facts_git.py``
  implements with the Git plumbing.
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


def __getattr__(name: str) -> typ.Any:  # noqa: ANN401 - module __getattr__ returns the re-exported object
    """Resolve the public ``main`` re-export on first use."""
    if name == "main":
        from .hook import main as hook_main

        return hook_main
    message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(message)
