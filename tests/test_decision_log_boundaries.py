"""The decision-log domain imports without its Git or storage adapters.

The durable log separates domain units (the run orchestrator, the write policy,
the record shapes and ports, and the settings resolver) from adapters (the
JSONL sink, the locked appender, the signal guard, and the Git-backed
composition).

An import-time dependency cannot be detected reliably from inside a process
that has already loaded the adapter modules, so this test proves the boundary
in a fresh interpreter and inspects ``sys.modules`` there.
"""

from __future__ import annotations

import ast
import json
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
import typing as typ
from pathlib import Path

# The repository root is the working directory of the child interpreter, so its
# ``sys.path`` entry resolves the package under test rather than an installed
# copy.
_REPO_ROOT = Path(__file__).resolve().parents[1]

_DOMAIN_MODULES = (
    "post_turn_quality_stop_hook.decision_log.log",
    "post_turn_quality_stop_hook.decision_log.policy",
)

#: Modules a domain unit must not reach. ``git`` is the Git adapter, and the
#: location split adds its own units: ``git_location_paths`` holds the path
#: rules, ``git_location_query`` runs the Git commands, and ``git_location``
#: composes both into the ownership answers. ``sink``, ``append``,
#: ``composition``, ``ownership``, ``platform_support``, ``retention``, and
#: ``adapters`` are the filesystem storage adapters; ``clock`` and ``signals``
#: are the process adapters; ``records`` and ``diagnostics`` are the
#: process-facing shape and warning modules; ``hook_composition`` is the
#: production composition root that binds them all.
_FORBIDDEN_MODULES = (
    "post_turn_quality_stop_hook.git",
    "post_turn_quality_stop_hook.git_location",
    "post_turn_quality_stop_hook.git_location_paths",
    "post_turn_quality_stop_hook.git_location_query",
    "post_turn_quality_stop_hook.hook_composition",
    "post_turn_quality_stop_hook.diagnostics",
    "post_turn_quality_stop_hook.decision_log.adapters",
    "post_turn_quality_stop_hook.decision_log.append",
    "post_turn_quality_stop_hook.decision_log.clock",
    "post_turn_quality_stop_hook.decision_log.composition",
    "post_turn_quality_stop_hook.decision_log.ownership",
    "post_turn_quality_stop_hook.decision_log.platform_support",
    "post_turn_quality_stop_hook.decision_log.records",
    "post_turn_quality_stop_hook.decision_log.retention",
    "post_turn_quality_stop_hook.decision_log.signals",
    "post_turn_quality_stop_hook.decision_log.sink",
)

#: Module-level names that would let a domain unit read the process behind the
#: caller's back. ``os.getpid`` reads the process, ``sys.stderr`` writes a
#: diagnostic outside the warning channel, and ``signal`` reads the platform's
#: signal table or changes a disposition.
_FORBIDDEN_PROCESS_NAMES = frozenset({"os", "sys", "signal"})

#: The domain sources that must not read those process globals.
_DOMAIN_SOURCES = (
    _REPO_ROOT / "post_turn_quality_stop_hook" / "decision_log" / "log.py",
    _REPO_ROOT / "post_turn_quality_stop_hook" / "decision_log" / "policy.py",
)


def _imported_modules() -> list[str]:
    """Import the domain modules in a fresh interpreter and list its modules."""
    script = (
        "import json, sys\n"
        "import post_turn_quality_stop_hook.decision_log.log\n"
        "import post_turn_quality_stop_hook.decision_log.policy\n"
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


def test_the_domain_modules_import_without_adapters() -> None:
    """Importing the log and policy never loads a Git or storage adapter."""
    imported = set(_imported_modules())

    assert set(_DOMAIN_MODULES) <= imported, (
        "the fresh interpreter must have loaded the domain modules"
    )
    reached = sorted(set(_FORBIDDEN_MODULES) & imported)
    assert reached == [], (
        f"the decision-log domain must not import its adapters, but loaded {reached!r}"
    )


def _process_reads(source: Path) -> set[str]:
    """Return where ``source`` imports or calls a forbidden process global.

    The check is syntactic rather than runtime-based: ``os``, ``sys`` and
    ``signal`` are always loaded in an interpreter, so an import-graph check
    cannot see a domain unit reaching them. Walking the syntax tree catches an
    import anywhere in the module and any attribute access that names one.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    reads: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            reads.update(
                alias.name
                for alias in node.names
                if alias.name in _FORBIDDEN_PROCESS_NAMES
            )
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and (
                node.module.split(".", 1)[0] in _FORBIDDEN_PROCESS_NAMES
            ):
                reads.add(node.module)
        elif isinstance(node, ast.Attribute):
            owner = node.value
            if isinstance(owner, ast.Name) and owner.id in _FORBIDDEN_PROCESS_NAMES:
                reads.add(f"{owner.id}.{node.attr}")
    return reads


def test_the_domain_modules_do_not_read_process_globals() -> None:
    """The log and policy never read the process, standard error, or signals.

    Those are the concerns the composition root binds instead: the clock, the
    process identifier, the warning channel, the signal-name table, and the
    terminal-commit mask.
    """
    reached = {
        str(source.relative_to(_REPO_ROOT)): sorted(_process_reads(source))
        for source in _DOMAIN_SOURCES
    }

    assert all(reads == [] for reads in reached.values()), (
        f"a decision-log domain unit read a process global: {reached!r}"
    )


def test_the_facade_exposes_no_adapter_clock_or_storage_modules() -> None:
    """The package facade keeps the clock adapter out of the domain namespace.

    ``SystemClock`` reads the process clocks, so it belongs with the adapters
    a composition root binds. Importing the facade must expose neither the
    name nor the modules that would let a domain unit reach the process: the
    concrete clock, the signal guard, and the JSONL storage sink.
    """
    script = (
        "import json, sys\n"
        "import post_turn_quality_stop_hook.decision_log as facade\n"
        "print(json.dumps({\n"
        "    'has_system_clock': hasattr(facade, 'SystemClock'),\n"
        "    'adapter_clock': "
        "'post_turn_quality_stop_hook.decision_log.clock' in sys.modules,\n"
        "    'signals': "
        "'post_turn_quality_stop_hook.decision_log.signals' in sys.modules,\n"
        "    'sink': "
        "'post_turn_quality_stop_hook.decision_log.sink' in sys.modules,\n"
        "}))\n"
    )
    completed = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: fixed argv, no shell.
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    observed = typ.cast("dict[str, bool]", json.loads(completed.stdout))
    assert observed == {
        "has_system_clock": False,
        "adapter_clock": False,
        "signals": False,
        "sink": False,
    }, f"the facade imported an adapter: {observed!r}"


def test_the_adapter_surface_is_still_importable() -> None:
    """The adapters remain reachable from their explicit module.

    The boundary test above would also pass if the adapters were broken, so
    this checks the other side of the split: a composition root can still bind
    the concrete sink and signal guard.
    """
    script = (
        "from post_turn_quality_stop_hook.decision_log.adapters import ( \n"
        "    JsonlRecordSink,\n"
        "    LockedJsonlAppender,\n"
        "    SignalGuard,\n"
        "    default_jsonl_sink,\n"
        "    default_jsonl_sink_for,\n"
        "    prune_with_defaults,\n"
        "    signal_name,\n"
        "    terminal_commit,\n"
        ")\n"
        "print('ok')\n"
    )
    completed = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: fixed argv, no shell.
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "ok"
