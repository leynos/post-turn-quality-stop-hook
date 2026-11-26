"""The branch-gate domain imports without its Git, GitHub or driver adapters.

The gates decide from ports, so importing ``branch_gates`` must load no Git
query, no GitHub client, no build-driver adapter, no template renderer, and no
subprocess module. Those adapters are bound once at the composition edge, which
is what lets a unit test decide every gate from prepared answers.

An import-time dependency cannot be detected reliably from inside a process
that has already loaded the adapter modules, so this test proves the boundary in
a fresh interpreter and inspects ``sys.modules`` there.
"""

from __future__ import annotations

import json
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
import typing as typ
from pathlib import Path

# The repository root is the working directory of the child interpreter, so its
# ``sys.path`` entry resolves the package under test rather than an installed
# copy.
_REPO_ROOT = Path(__file__).resolve().parents[1]

_DOMAIN_MODULE = "post_turn_quality_stop_hook.branch_gates"

#: Modules the gate module must not reach at import time. ``git``, ``github``,
#: ``driver`` and ``process`` are adapters; ``formatting`` and ``templates``
#: render payloads; ``hook`` and ``hook_composition`` are production composition
#: roots that bind the adapters.
_FORBIDDEN_MODULES = (
    "post_turn_quality_stop_hook.git",
    "post_turn_quality_stop_hook.github",
    "post_turn_quality_stop_hook.driver",
    "post_turn_quality_stop_hook.formatting",
    "post_turn_quality_stop_hook.hook",
    "post_turn_quality_stop_hook.hook_composition",
    "post_turn_quality_stop_hook.process",
    "post_turn_quality_stop_hook.templates",
    "subprocess",
)

#: Domain modules the gate module is expected to load, so the boundary test
#: would also notice a gate that stopped importing its own presentation rule.
#: ``branch_state_port`` is imported only for its type annotations, so it is
#: deliberately absent from the fresh interpreter's module table; the syntax
#: check in ``test_pipeline.py`` pins that import.
_EXPECTED_MODULES = ("post_turn_quality_stop_hook.upstream_label",)


def _imported_modules() -> list[str]:
    """Import the gate module in a fresh interpreter and list its modules."""
    script = (
        "import json, sys\n"
        "import post_turn_quality_stop_hook.branch_gates\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    completed = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: fixed argv, no shell.
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return typ.cast("list[str]", json.loads(completed.stdout))


def test_the_gate_module_imports_without_adapters() -> None:
    """Importing the gate module never loads an adapter or renderer."""
    imported = set(_imported_modules())

    assert _DOMAIN_MODULE in imported, (
        "the fresh interpreter must have loaded the gate module"
    )
    reached = sorted(set(_FORBIDDEN_MODULES) & imported)
    assert reached == [], (
        f"the gate domain must not import its adapters, but loaded {reached!r}"
    )
    assert set(_EXPECTED_MODULES) <= imported, (
        "the gate module must import its ports, but missed"
        f" {sorted(set(_EXPECTED_MODULES) - imported)!r}"
    )
