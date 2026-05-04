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
This module is a thin re-export layer. All implementation lives in
``hook.py``, which contains the git plumbing, driver selection, build
execution, formatting utilities, and the top-level orchestration logic
(``prepare_run_stop_checks`` and ``run_stop_checks``). Exposing only
``main`` here keeps the package entry point clean while allowing
consumers to import internals directly from ``post_turn_quality_stop_hook.hook``
when needed.
"""

from __future__ import annotations

from .hook import main

__all__ = ["main"]
