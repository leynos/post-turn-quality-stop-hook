"""Durable per-invocation decision log for the stop hook.

The log is an append-only JSON Lines file per UTC day beneath the XDG state
directory. Each hook invocation writes:

- one ``start`` record carrying correlation fields;
- ``progress`` records as stages and commands begin and finish; and
- one ``finish`` record carrying the decision, timings, repository facts,
  gate outcomes, and command results.

A run with a ``start`` record but no ``finish`` record was interrupted. The
log never carries command output, environment contents, or hook payload text,
and a failed write only warns on standard error so the hook decision is never
changed.

Two surfaces
------------

This module exposes the **domain-facing** names only: the record and decision
shapes, the policies, the collaborator protocols, the settings resolver, and
the orchestrator that writes through a record sink. Importing it therefore
loads no Git query and no filesystem adapter.

The concrete storage and process adapters live in
:mod:`post_turn_quality_stop_hook.decision_log.adapters`: ``JsonlRecordSink``,
``LockedJsonlAppender``, ``SignalGuard``, ``default_jsonl_sink``,
``prune_old_logs``, ``prune_with_defaults``, ``signal_name`` and
``terminal_commit``. Only a composition root should need them; a domain unit
imports this module instead.

The package keeps its concerns separate: :mod:`.settings` resolves
configuration, :mod:`.bounds` bounds the free text a record may carry,
:mod:`.timing` holds the pure elapsed-time arithmetic, :mod:`.ownership`
recognises files the hook wrote, :mod:`.retention` deletes expired ones,
:mod:`.policy` decides whether a file may be used, :mod:`.append` performs the
locked single write, :mod:`.sink` composes those parts, :mod:`.assembly` turns
collected facts into records, :mod:`.stats` measures storage, :mod:`.log`
orchestrates a run, and :mod:`.signals` records interruptions and masks them
around a terminal commit. The pipeline depends on the domain-shaped
:class:`~post_turn_quality_stop_hook.recording.DecisionRecorder` protocol.

The time-source and record-shape modules (``clock`` and ``records``) and the
names they export are resolved lazily. A caller that imports a domain unit
therefore loads neither the time source nor the record-shape module behind its
back, which keeps the import boundary the same whether a name is reached
through this namespace or through its own module.
"""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook.decision_log.assembly import (
    GATE_ERROR_CATEGORY,
    RunRecord,
)
from post_turn_quality_stop_hook.decision_log.constants import (
    DEFAULT_RETENTION_DAYS,
    FALSY_TOKENS,
    GATE_ORDER,
    HOOK_VERSION,
    LOG_FILE_PREFIX,
    LOG_FILE_SUFFIX,
    LOG_SUBDIR,
    MAX_REASON_CHARS,
    PRODUCER,
    RECORD_TYPES,
    SCHEMA_VERSION,
    TRUTHY_TOKENS,
)
from post_turn_quality_stop_hook.decision_log.log import (
    DecisionLog,
    make_decision_log,
)
from post_turn_quality_stop_hook.decision_log.policy import (
    FileClaim,
    FileIdentity,
    FileOwnership,
    RefusalReason,
    WritePolicy,
    WriteVerdict,
)
from post_turn_quality_stop_hook.decision_log.schedule import RetentionSchedule
from post_turn_quality_stop_hook.decision_log.settings import (
    DecisionLogSettings,
    log_directory,
    parse_retention_days,
    settings_from_env,
)
from post_turn_quality_stop_hook.decision_log.stats import StorageStats

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook.decision_log.clock import Clock, SystemClock
    from post_turn_quality_stop_hook.decision_log.records import (
        InvocationContext,
        RecordSink,
    )

#: Domain-facing exports. Adapter names are exported by
#: :mod:`post_turn_quality_stop_hook.decision_log.adapters` instead, so a
#: domain unit can import this module without loading Git or filesystem code.
__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "FALSY_TOKENS",
    "GATE_ERROR_CATEGORY",
    "GATE_ORDER",
    "HOOK_VERSION",
    "LOG_FILE_PREFIX",
    "LOG_FILE_SUFFIX",
    "LOG_SUBDIR",
    "MAX_REASON_CHARS",
    "PRODUCER",
    "RECORD_TYPES",
    "SCHEMA_VERSION",
    "TRUTHY_TOKENS",
    "Clock",
    "DecisionLog",
    "DecisionLogSettings",
    "FileClaim",
    "FileIdentity",
    "FileOwnership",
    "InvocationContext",
    "RecordSink",
    "RefusalReason",
    "RetentionSchedule",
    "RunRecord",
    "StorageStats",
    "SystemClock",
    "WritePolicy",
    "WriteVerdict",
    "log_directory",
    "make_decision_log",
    "parse_retention_days",
    "settings_from_env",
]

#: Names whose module must stay out of an eager import. They live in the time
#: source and in the record-shape module, which a domain import must not pull
#: in merely because a caller imported this package.
_LAZY_EXPORTS = {  # ruff:ignore[non-empty-init-module]  # a lazy map is the module's re-export table.
    "Clock": "post_turn_quality_stop_hook.decision_log.clock",
    "SystemClock": "post_turn_quality_stop_hook.decision_log.clock",
    "InvocationContext": "post_turn_quality_stop_hook.decision_log.records",
    "RecordSink": "post_turn_quality_stop_hook.decision_log.records",
}


def __getattr__(name: str) -> typ.Any:  # noqa: ANN401 - module __getattr__ returns the lazily imported object
    """Resolve one lazily exported name on first use.

    Raises
    ------
    AttributeError
        When ``name`` is not one of this package's lazy exports.

    """
    import importlib

    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        message = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(message)
    module = importlib.import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value
