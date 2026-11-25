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

The package keeps its concerns separate: :mod:`.settings` resolves
configuration, :mod:`.ownership` recognises files the hook wrote,
:mod:`.retention` deletes expired ones, :mod:`.policy` decides whether a file
may be used, :mod:`.append` performs the locked single write, :mod:`.sink`
composes those parts, :mod:`.assembly` turns collected facts into records,
:mod:`.stats` measures storage, :mod:`.log` orchestrates a run, and
:mod:`.signals` records interruptions and masks them around a terminal
commit. The pipeline depends on the domain-shaped
:class:`~post_turn_quality_stop_hook.recording.DecisionRecorder` protocol;
:class:`DecisionLog` collects records and delegates storage to a
:class:`RecordSink`, and :class:`SignalGuard` installs termination handlers.
"""

from __future__ import annotations

import os as os
import signal as signal
from pathlib import Path as Path

from post_turn_quality_stop_hook.decision_log.append import LockedJsonlAppender
from post_turn_quality_stop_hook.decision_log.assembly import GATE_ERROR_CATEGORY
from post_turn_quality_stop_hook.decision_log.clock import Clock, SystemClock
from post_turn_quality_stop_hook.decision_log.composition import (
    default_jsonl_sink,
    prune_with_defaults,
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
    RefusalReason,
    WritePolicy,
    WriteVerdict,
)
from post_turn_quality_stop_hook.decision_log.records import (
    InvocationContext,
    RecordSink,
)
from post_turn_quality_stop_hook.decision_log.records import _warn as _warn
from post_turn_quality_stop_hook.decision_log.retention import prune_old_logs
from post_turn_quality_stop_hook.decision_log.settings import (
    DecisionLogSettings,
    log_directory,
    parse_retention_days,
    settings_from_env,
)
from post_turn_quality_stop_hook.decision_log.signals import SignalGuard
from post_turn_quality_stop_hook.decision_log.sink import JsonlRecordSink
from post_turn_quality_stop_hook.decision_log.stats import StorageStats

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
    "InvocationContext",
    "JsonlRecordSink",
    "LockedJsonlAppender",
    "RecordSink",
    "RefusalReason",
    "SignalGuard",
    "StorageStats",
    "SystemClock",
    "WritePolicy",
    "WriteVerdict",
    "default_jsonl_sink",
    "log_directory",
    "make_decision_log",
    "parse_retention_days",
    "prune_old_logs",
    "prune_with_defaults",
    "settings_from_env",
]
