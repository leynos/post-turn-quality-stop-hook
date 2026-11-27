"""Measure the storage a record sink attempts, without storing anything.

The JSONL sink appends and prunes; this wrapper is the only unit that measures
those attempts. Keeping the two apart means the sink can be substituted for one
that touches no file, and this wrapper can be substituted for one that keeps no
figures, without either unit standing in for the other. The collector is shared
with :class:`~.log.DecisionLog`, which writes its own storage record from it.

The wrapper reports a storage failure with a fixed, path-free message and a
bounded category. An injected sink may raise anything, so the guard belongs
here rather than in every caller: the log writes its terminal record from a
signal handler and inside a ``finally`` block, where an escaping failure could
replace a decision that was already determined.
"""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook.decision_log.records import RecordTooLargeError
from post_turn_quality_stop_hook.decision_log.stats import StorageStats, error_category
from post_turn_quality_stop_hook.decision_log.timing import _elapsed_ms

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from post_turn_quality_stop_hook.decision_log.clock import Clock
    from post_turn_quality_stop_hook.decision_log.records import StorageSink


class MeasuredRecordSink:
    """Measure every append and prune a wrapped sink attempts.

    Parameters
    ----------
    sink
        Storage unit that appends and prunes validated records.
    clock
        Time source for the attempt timings. It is required, so the wrapper
        never reads the process clock behind its caller's back.
    stats
        Collector the timings and failure categories are accumulated in. It is
        required, so the log and this wrapper can share one collector.
    on_error
        Callback for storage failures. It receives fixed text with no path and
        no exception detail.

    """

    def __init__(
        self,
        sink: StorageSink,
        *,
        clock: Clock,
        stats: StorageStats,
        on_error: cabc.Callable[[str], None],
    ) -> None:
        """Store the wrapped sink and the measurement collaborators."""
        self._sink = sink
        self._clock = clock
        self._stats = stats
        self._on_error = on_error

    def storage_stats(self) -> dict[str, dict[str, object]]:
        """Return bounded timings and failure categories for the finish record."""
        return self._stats.snapshot()

    def write(self, record: cabc.Mapping[str, object]) -> None:
        """Append one record, recording its timings and failure categories.

        Every attempt is counted, however it ends. A record that was refused,
        dropped for its size, or lost to a storage failure cannot change the
        decision, which is already computed.
        """
        started = self._clock.monotonic()
        self._stats.start_write()
        lock_wait_ms = 0
        try:
            lock_wait_ms = self._sink.write(record)
        except OSError as exc:
            self._report_write_failure(error_category(exc))
        except RecordTooLargeError as exc:
            # A record over the writer's bound would make the whole daily file
            # foreign to every later run and to retention, so it is dropped.
            self._report_write_failure(error_category(exc))
        finally:
            self._stats.add_write(
                storage_ms=_elapsed_ms(started, self._clock.monotonic()),
                lock_wait_ms=lock_wait_ms,
            )

    def prune(self, today: dt.date, retention_days: int) -> None:
        """Prune expired files, recording the attempt and any failure.

        A sweep that reports failures has already warned, so the wrapper only
        counts them; an unexpected exception is counted and reported here.
        """
        self._stats.start_prune()
        started = self._clock.monotonic()
        try:
            report = self._sink.prune(today, retention_days)
        except OSError as exc:
            category = error_category(exc)
            self._stats.fail_prune(category)
            self._on_error(f"could not prune the decision log ({category})")
        else:
            for _failure in range(report.failures):
                self._stats.fail_prune("OSError")
        finally:
            self._stats.add_prune(
                storage_ms=_elapsed_ms(started, self._clock.monotonic())
            )

    def _report_write_failure(self, category: str) -> None:
        """Count one storage failure and report its bounded category."""
        self._stats.fail_write(category)
        self._on_error(f"could not write the decision log ({category})")
