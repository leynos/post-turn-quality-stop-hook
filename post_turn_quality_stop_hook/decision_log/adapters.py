"""Adapter-facing names for the decision log.

Importing this module loads the JSONL storage adapters and the Git location
queries behind the default sink's policy. A domain unit must not import it:
:mod:`post_turn_quality_stop_hook.decision_log` exports the domain-facing names,
and this module exports the concrete adapters a composition root binds.

The split is explicit so an import check can prove that orchestrating,
assembling, and deciding a record never drags in the Git or filesystem code.
Production composes these adapters once, in
:mod:`post_turn_quality_stop_hook.hook_composition` and
:mod:`post_turn_quality_stop_hook.decision_log.composition`.

The retention sweep, ``prune_old_logs``, is exported here too. Its queries are
injected ports, but the sweep itself opens, verifies, and deletes files, so it
belongs with the adapters rather than the domain-facing package namespace.

``SystemClock`` is exported here for the same reason: it reads the process'
wall and monotonic clocks, so only a composition root should bind it. The
domain-facing package keeps the ``Clock`` protocol that a unit depends on.
"""

from __future__ import annotations

from post_turn_quality_stop_hook.decision_log.append import (
    AppendPolicy,
    AppendResult,
    LockedJsonlAppender,
)
from post_turn_quality_stop_hook.decision_log.clock import SystemClock
from post_turn_quality_stop_hook.decision_log.composition import (
    default_jsonl_sink,
    default_jsonl_sink_for,
    prune_with_defaults,
)
from post_turn_quality_stop_hook.decision_log.retention import prune_old_logs
from post_turn_quality_stop_hook.decision_log.signals import (
    SignalGuard,
    signal_name,
    terminal_commit,
)
from post_turn_quality_stop_hook.decision_log.sink import JsonlRecordSink
from post_turn_quality_stop_hook.decision_log.telemetry import MeasuredRecordSink

#: Adapter-facing exports. A domain unit imports
#: :mod:`post_turn_quality_stop_hook.decision_log` instead.
__all__ = [
    "AppendPolicy",
    "AppendResult",
    "JsonlRecordSink",
    "LockedJsonlAppender",
    "MeasuredRecordSink",
    "SignalGuard",
    "SystemClock",
    "default_jsonl_sink",
    "default_jsonl_sink_for",
    "prune_old_logs",
    "prune_with_defaults",
    "signal_name",
    "terminal_commit",
]
