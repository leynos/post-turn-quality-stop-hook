"""Composition of the decision log with the real Git queries.

The decision-log units take their Git queries as explicit arguments, so no
constructor holds a silent default that might be more or less permissive than a
caller meant. Production needs the real queries bound once, per run. This module
is that single named composition point: it is the only place in the package that
imports both a decision-log adapter and the Git query module to wire them
together. :func:`default_jsonl_sink_for` is therefore the one factory that turns
resolved settings into the adapter a log writes through; ``log.py`` receives
that sink rather than building one.

Nothing here decides anything. Each function supplies the concrete queries and
returns the composed object, so a test can substitute narrow callables at the
unit under test while production keeps one obvious wiring site.
"""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook.decision_log.records import _warn
from post_turn_quality_stop_hook.decision_log.retention import prune_old_logs
from post_turn_quality_stop_hook.decision_log.settings import log_directory
from post_turn_quality_stop_hook.decision_log.sink import JsonlRecordSink
from post_turn_quality_stop_hook.git_location import (
    is_ignored,
    is_in_worktree,
    is_tracked,
    is_tracked_entry,
    would_appear_as_change,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    from post_turn_quality_stop_hook.decision_log.clock import Clock
    from post_turn_quality_stop_hook.decision_log.settings import DecisionLogSettings
    from post_turn_quality_stop_hook.decision_log.stats import StorageStats


def default_jsonl_sink(
    log_dir: Path,
    *,
    clock: Clock | None = None,
    on_error: cabc.Callable[[str], None] | None = None,
    stats: StorageStats | None = None,
) -> JsonlRecordSink:
    """Build the JSONL sink wired to the real Git location queries.

    Parameters
    ----------
    log_dir
        Directory holding the daily log files.
    clock
        Time source. ``None`` lets the sink choose the system clock.
    on_error
        Callback for storage failures. ``None`` keeps the sink's default.
    stats
        Telemetry collector. ``None`` lets the sink create a private one.

    Returns
    -------
    JsonlRecordSink
        A sink whose policy asks Git itself whether a daily file is tracked or
        could appear as a worktree change.

    """
    return JsonlRecordSink(
        log_dir,
        is_tracked=is_tracked,
        is_visible=would_appear_as_change,
        is_tracked_entry=is_tracked_entry,
        in_worktree=is_in_worktree,
        is_ignored=is_ignored,
        clock=clock,
        on_error=_warn if on_error is None else on_error,
        stats=stats,
    )


def default_jsonl_sink_for(
    settings: DecisionLogSettings,
    *,
    clock: Clock | None = None,
    on_error: cabc.Callable[[str], None] | None = None,
    stats: StorageStats | None = None,
) -> JsonlRecordSink:
    """Build the sink the run writes through, beneath its resolved state home.

    This is the composition root's sink factory: it is the only place the log
    directory is derived from settings, so ``log.py`` never has to know either
    the XDG layout or the Git queries behind the policy.

    Parameters
    ----------
    settings
        Resolved log settings whose ``state_home`` locates the log directory.
    clock
        Time source. ``None`` lets the sink choose the system clock.
    on_error
        Callback for storage failures. ``None`` keeps the sink's default.
    stats
        Telemetry collector. ``None`` lets the sink create a private one.

    Returns
    -------
    JsonlRecordSink
        A sink writing beneath ``log_directory(settings.state_home)``.

    """
    return default_jsonl_sink(
        log_directory(settings.state_home),
        clock=clock,
        on_error=on_error,
        stats=stats,
    )


def prune_with_defaults(
    log_dir: Path,
    retention_days: int,
    *,
    today: dt.date,
    on_error: cabc.Callable[[str], None] | None = None,
) -> int:
    """Prune expired daily files through the real Git ownership queries.

    This is the production entry point for retention; it exists so the four
    ownership queries have exactly one place that binds them to the real
    implementation rather than four silent defaults inside ``prune_old_logs``.

    Parameters
    ----------
    log_dir
        Directory containing ``hook-YYYY-MM-DD.jsonl`` files.
    retention_days
        Number of days to retain. Zero keeps only today's file.
    today
        Current date, supplied by the run's clock.
    on_error
        Callback for scan and deletion failures.

    Returns
    -------
    int
        Number of files removed.

    """
    return prune_old_logs(
        log_dir,
        retention_days,
        today=today,
        is_tracked=is_tracked,
        is_tracked_entry=is_tracked_entry,
        in_worktree=is_in_worktree,
        is_ignored=is_ignored,
        on_error=on_error,
    )
