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

import os
import typing as typ

from post_turn_quality_stop_hook.decision_log.clock import SystemClock
from post_turn_quality_stop_hook.decision_log.retention import prune_old_logs
from post_turn_quality_stop_hook.decision_log.settings import log_directory
from post_turn_quality_stop_hook.decision_log.sink import JsonlRecordSink
from post_turn_quality_stop_hook.diagnostics import warn
from post_turn_quality_stop_hook.git_location import (
    GitLocationQueries,
    queries_for,
)
from post_turn_quality_stop_hook.git_location_query import GitLocationAdapter

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    from post_turn_quality_stop_hook.decision_log.clock import Clock
    from post_turn_quality_stop_hook.decision_log.settings import DecisionLogSettings
    from post_turn_quality_stop_hook.decision_log.stats import StorageStats


def location_queries(
    environ: cabc.Mapping[str, str] | None = None,
) -> GitLocationQueries:
    """Bind the real Git location adapter for one environment snapshot.

    This is the only place the adapter is constructed, so a caller that must
    not read the process environment supplies the snapshot it already read.
    ``None`` reads ``os.environ``, which keeps a direct caller working without
    a composition step.
    """
    return queries_for(
        GitLocationAdapter(environ=os.environ if environ is None else environ)
    )


def default_jsonl_sink(  # ruff:ignore[too-many-arguments]  # the log directory and each collaborator the composition root binds is a distinct input.
    log_dir: Path,
    *,
    clock: Clock | None = None,
    on_error: cabc.Callable[[str], None] | None = None,
    stats: StorageStats | None = None,
    location: GitLocationQueries | None = None,
    environ: cabc.Mapping[str, str] | None = None,
) -> JsonlRecordSink:
    """Build the JSONL sink wired to the real Git location queries.

    This is the one place the sink's process-facing collaborators are bound:
    the system clock, the standard-error writer, and the Git location queries.
    Every other caller supplies them, so a domain unit never reads a clock or a
    warning stream itself.

    Parameters
    ----------
    log_dir
        Directory holding the daily log files.
    clock
        Time source. ``None`` binds the system clock.
    on_error
        Callback for storage and retention failures. ``None`` binds the
        standard-error writer.
    stats
        Telemetry collector. ``None`` lets the sink create a private one.
    location
        Bound Git ownership questions. ``None`` binds the real adapter from
        ``environ``.
    environ
        Environment snapshot the Git queries are decided from. ``None`` reads
        this process's environment; the composition root supplies the
        invocation's snapshot instead.

    Returns
    -------
    JsonlRecordSink
        A sink whose policy asks Git itself whether a daily file is tracked or
        could appear as a worktree change.

    """
    queries = location if location is not None else location_queries(environ)
    return JsonlRecordSink(
        log_dir,
        is_tracked=queries.is_tracked,
        is_visible=queries.is_visible,
        is_tracked_entry=queries.is_tracked_entry,
        in_worktree=queries.in_worktree,
        is_ignored=queries.is_ignored,
        clock=SystemClock() if clock is None else clock,
        on_error=warn if on_error is None else on_error,
        stats=stats,
    )


def default_jsonl_sink_for(  # ruff:ignore[too-many-arguments]  # settings and each collaborator the composition root binds is a distinct input.
    settings: DecisionLogSettings,
    *,
    clock: Clock | None = None,
    on_error: cabc.Callable[[str], None] | None = None,
    stats: StorageStats | None = None,
    location: GitLocationQueries | None = None,
    environ: cabc.Mapping[str, str] | None = None,
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
        Time source. ``None`` binds the system clock.
    on_error
        Callback for storage and retention failures. ``None`` binds the
        standard-error writer.
    stats
        Telemetry collector. ``None`` lets the sink create a private one.
    location
        Bound Git ownership questions. ``None`` binds the real adapter from
        ``environ``.
    environ
        Environment snapshot the Git queries are decided from.

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
        location=location,
        environ=environ,
    )


def prune_with_defaults(  # ruff:ignore[too-many-arguments]  # directory, window, date and each bound query is a distinct input.
    log_dir: Path,
    retention_days: int,
    *,
    today: dt.date,
    on_error: cabc.Callable[[str], None] | None = None,
    location: GitLocationQueries | None = None,
    environ: cabc.Mapping[str, str] | None = None,
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
    location
        Bound Git ownership questions. ``None`` binds the real adapter from
        ``environ``.
    environ
        Environment snapshot the Git queries are decided from.

    Returns
    -------
    int
        Number of files removed.

    """
    queries = location if location is not None else location_queries(environ)
    return prune_old_logs(
        log_dir,
        retention_days,
        today=today,
        is_tracked=queries.is_tracked,
        is_tracked_entry=queries.is_tracked_entry,
        in_worktree=queries.in_worktree,
        is_ignored=queries.is_ignored,
        on_error=on_error,
    )
