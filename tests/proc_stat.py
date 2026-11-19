"""Shared parser for the ``/proc/<pid>/stat`` process state field.

The liveness probe in ``test_process`` and the parser tests in
``test_process_state`` must read the same implementation. A separate copy in
each file would let a regression in the probe's copy leave the parser tests
green.
"""

from __future__ import annotations


def process_state(stat_line: str) -> str | None:
    """Return the state letter from one ``/proc/<pid>/stat`` line.

    The line reads ``pid (comm) state ...``, and ``comm`` is the process'
    self-chosen name, which may contain spaces and parentheses. Splitting the
    line on whitespace therefore cannot locate the state field; it starts
    after the final ``)``. ``None`` means the line could not be parsed.
    """
    close = stat_line.rfind(")")
    if close == -1:
        return None
    remainder = stat_line[close + 1 :].split()
    return remainder[0] if remainder else None
