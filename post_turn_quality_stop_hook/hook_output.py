"""Adapter between the stop checks' printed hook JSON and the breaker.

The stop checks report a blocking decision by printing Claude Code hook JSON
to standard output. The failure breaker works on a ``CheckResult`` instead, so
this module captures that output, forwards it unchanged, and translates it
into the domain outcome. It also renders the breaker's short-circuit message,
keeping the hook's wire format out of the breaker policy.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import typing as typ

from post_turn_quality_stop_hook.failure_breaker import CheckOutcome, CheckResult

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def is_block_output(output: str) -> bool:
    """Return whether hook output contains a blocking decision."""
    for line in output.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("decision") == "block":
            return True
    return False


def capture_check_result(run: cabc.Callable[[], int]) -> CheckResult:
    """Run checks that print hook JSON and report their ``CheckResult``.

    Whatever the checks print is forwarded to standard output even if they
    raise, so the breaker never hides output the hook would otherwise emit.
    """
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            exit_code = run()
    finally:
        sys.stdout.write(buffer.getvalue())
    outcome = (
        CheckOutcome.BLOCKED
        if is_block_output(buffer.getvalue())
        else CheckOutcome.PASSED
    )
    return CheckResult(exit_code=exit_code, outcome=outcome)


def short_circuit_message(previous: int) -> str:
    """Return the hook JSON that allows the stop and explains why."""
    message = (
        f"post-turn-quality-stop-hook blocked {previous} consecutive times in "
        "this directory, so this stop was allowed without running the checks. "
        "Checks resume on the next stop."
    )
    return json.dumps({"systemMessage": message})
