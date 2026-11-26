"""Assemble one invocation's records from the facts the hook reports.

Stage timings, gate outcomes, and the run's final decision are collected here
and turned into the record shapes the sink persists. Keeping this apart from
:class:`~post_turn_quality_stop_hook.decision_log.log.DecisionLog` means record
assembly can be exercised without a sink, a directory, or retention, and the
log can orchestrate storage without knowing how a finish record is shaped.
"""

from __future__ import annotations

import json
import typing as typ

from post_turn_quality_stop_hook.decision_log.bounds import _bounded_reason
from post_turn_quality_stop_hook.decision_log.constants import (
    GATE_ORDER,
    MAX_RECORD_BYTES,
    RECORD_HEADROOM_BYTES,
)
from post_turn_quality_stop_hook.decision_log.timing import _elapsed_ms

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from post_turn_quality_stop_hook.decision_log.clock import Clock

# A finish record must stay verifiable by the same reader that checks a daily
# file before appending to it. The writer therefore bounds its own record well
# below the reader's per-line limit and drops optional sections, in this order,
# rather than emit a line the reader would treat as foreign content.
_RECORD_LIMIT = MAX_RECORD_BYTES - RECORD_HEADROOM_BYTES
_OMITTABLE_SECTIONS = ("commands", "quality", "repository", "stages")
_OMITTED_FIELD = "details_omitted"

#: Fixed value recorded when a gate could not query Git. The diagnostic a
#: query produces is command output, which may repeat a path, an environment
#: value, or a credential-bearing URL, so the durable record keeps only this
#: category. The decision object still carries the detail for its callers and
#: the standard-error telemetry.
GATE_ERROR_CATEGORY = "git query failed"


def dump_record(record: cabc.Mapping[str, object]) -> str:
    r"""Serialise one record as a single line of strict UTF-8 text.

    ``ensure_ascii=False`` keeps ordinary records readable, but a value decoded
    with ``surrogateescape`` cannot be encoded as strict UTF-8. Dropping such a
    record would lose the terminal decision, so serialisation falls back to
    ASCII escaping: a lone surrogate becomes a ``\\udcXX`` JSON escape and the
    line stays strict UTF-8. Both the size bound and the writer call this
    function, so the measured bytes are exactly the persisted bytes.
    """
    dumped = json.dumps(dict(record), ensure_ascii=False, separators=(",", ":"))
    try:
        dumped.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(dict(record), ensure_ascii=True, separators=(",", ":"))
    return dumped


class RunRecord:
    """Collect one invocation's facts and build its records.

    Parameters
    ----------
    clock
        Time source for stage and total durations.
    started
        Monotonic timestamp at which the hook process began its work.

    """

    def __init__(self, clock: Clock, *, started: float) -> None:
        """Start an empty run record."""
        self._clock = clock
        self._started = started
        # Startup is already complete by the time the log exists, so record it
        # as a finished stage rather than opening one that can never close.
        self._stages: dict[str, int] = {
            "startup": _elapsed_ms(self._started, self._clock.monotonic())
        }
        self._gates: dict[str, dict[str, object]] = {}
        self._repository: dict[str, object] = {}
        self._quality: dict[str, object] = {}
        self._commands: list[dict[str, object]] = []
        self._blocked = False
        self._block_reason: str | None = None

    def stage_finished(self, name: str, duration_ms: int) -> None:
        """Accumulate one stage's duration."""
        self._stages[name] = self._stages.get(name, 0) + duration_ms

    def gate(
        self,
        gate: str,
        outcome: str,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        """Record a gate outcome, its short reason, and whether Git failed.

        ``error`` is reduced to a closed category. A gate's failure text comes
        from a Git command and can carry command output, so it must never reach
        the durable log; its presence is still recorded, so a failed query
        stays distinguishable from a verified answer.
        """
        self._gates[gate] = {
            "gate": gate,
            "outcome": outcome,
            "reason": _bounded_reason(reason),
            "error": GATE_ERROR_CATEGORY if error is not None else None,
        }

    def set_repository(self, fields: cabc.Mapping[str, object]) -> None:
        """Record repository facts collected during preparation."""
        self._repository.update(fields)

    def set_quality(self, fields: cabc.Mapping[str, object]) -> None:
        """Record quality-gate selection facts."""
        self._quality.update(fields)

    def add_command(self, command: dict[str, object]) -> None:
        """Record one completed command."""
        self._commands.append(command)

    def block(self, reason: str | None) -> None:
        """Mark the invocation as having emitted a block decision."""
        self._blocked = True
        self._block_reason = _bounded_reason(reason)

    def finish(
        self,
        *,
        decision: str | None = None,
        exit_code: int = 0,
        error: str | None = None,
        signal_name: str | None = None,
    ) -> dict[str, object]:
        """Build the finish record, including gates never reached.

        Assembly is deliberately repeatable: the decision log decides whether a
        terminal record has already been *delivered*. Keeping the two apart
        lets a signal that arrives after assembly, while the record is still
        being written, deliver an ``interrupted`` record instead of finding the
        run finished and writing nothing.
        """
        resolved = decision or ("block" if self._blocked else "allow")
        record: dict[str, object] = {
            "record_type": "finish",
            "decision": resolved,
            "exit_code": int(exit_code),
            "error": _bounded_reason(error),
            "duration_ms": _elapsed_ms(self._started, self._clock.monotonic()),
            "stages": dict(self._stages),
            "repository": dict(self._repository),
            "quality": dict(self._quality),
            "gates": [
                self._gates.get(gate)
                or {"gate": gate, "outcome": "not_reached", "reason": None}
                for gate in GATE_ORDER
            ],
            "commands": list(self._commands),
        }
        if self._block_reason is not None:
            record["block_reason"] = self._block_reason
        if signal_name is not None:
            record["signal"] = signal_name
        return _bounded(record)


def _encoded_length(record: cabc.Mapping[str, object]) -> int:
    """Return the record's length once encoded as one JSON line.

    The measurement uses :func:`dump_record`, so an undecodable value is
    counted at the width of the escaped line the writer persists. Measuring
    differently would let a record pass the bound here and then be dropped as
    oversized by the sink.
    """
    return len(dump_record(record).encode("utf-8"))


def _omissions(record: cabc.Mapping[str, object]) -> list[str]:
    """Return the omissions an earlier bounding pass already recorded."""
    recorded = record.get(_OMITTED_FIELD)
    if not isinstance(recorded, list):
        return []
    return [name for name in recorded if isinstance(name, str)]


def _bounded(record: dict[str, object]) -> dict[str, object]:
    """Drop optional detail until the record fits one verifiable line.

    The terminal decision, the exit code, and the gate outcomes are never
    dropped: they are what the log exists to preserve. When detail has to go,
    the record names what was omitted so a reader can tell a small invocation
    from a truncated record.

    The label is part of the line, so it counts towards the limit it exists to
    meet; otherwise naming the last omission could itself push the record over
    and cost the decision. Bounding runs twice — once on the assembled record
    and once after the common fields are merged — so an earlier list is carried
    forward rather than replaced. If even the label cannot fit, it is left out
    so the record stays writable; the truncation is then visible only as absent
    detail.
    """
    omitted = _omissions(record)

    def fits(candidate: list[str]) -> bool:
        """Whether the record fits once ``candidate`` names its omissions."""
        trial = {key: value for key, value in record.items() if key != _OMITTED_FIELD}
        if candidate:
            trial[_OMITTED_FIELD] = candidate
        return _encoded_length(trial) <= _RECORD_LIMIT

    for section in _OMITTABLE_SECTIONS:
        if fits(omitted):
            break
        if section in record:
            del record[section]
            if section not in omitted:
                omitted.append(section)
    if omitted and fits(omitted):
        record[_OMITTED_FIELD] = omitted
    else:
        record.pop(_OMITTED_FIELD, None)
    return record
