"""Schedule one run's retention window against the sink it writes through.

This is the domain half of retention: deciding *when* to prune, once the
authoritative configuration has resolved the window. The filesystem half, the
sweep that deletes expired daily files, lives in :mod:`.retention` and runs
behind injected ownership queries. Keeping the schedule here means the run
orchestrator can wire retention without importing a filesystem adapter.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from post_turn_quality_stop_hook.decision_log.clock import Clock
    from post_turn_quality_stop_hook.decision_log.records import RecordSink


class RetentionSchedule:
    """Apply one run's retention window to the sink it writes through.

    The log is created before configuration is read, so it cannot know the
    configured window in its constructor. The composition edge invokes this
    helper once the authoritative configuration resolves the window, which
    keeps the scheduling decision out of record orchestration. A pruning
    failure is reported with a fixed warning and never changes the hook
    decision.

    Parameters
    ----------
    sink
        Persistence adapter whose expired files are removed.
    clock
        Time source supplying the current date.
    is_disabled
        Whether the run has opted out. An opt-out suppresses retention for the
        rest of the invocation.
    on_error
        Callback for pruning failures. It receives fixed text with no path and
        no exception detail. The composition layer supplies it, matching the
        sink and policy, so this helper holds no warning channel default of its
        own.

    """

    def __init__(
        self,
        sink: RecordSink,
        clock: Clock,
        *,
        is_disabled: cabc.Callable[[], bool],
        on_error: cabc.Callable[[str], None],
    ) -> None:
        """Store the sink, clock, opt-out state, and error sink for the run."""
        self._sink = sink
        self._clock = clock
        self._is_disabled = is_disabled
        self._on_error = on_error

    def apply(self, retention_days: int) -> None:
        """Prune expired daily files for the resolved window."""
        if self._is_disabled():
            return
        try:
            self._sink.prune(self._clock.wall().date(), retention_days)
        except Exception:  # ruff:ignore[blind-except]  # logging must never change the decision.
            self._on_error("could not prune the decision log")
