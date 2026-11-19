"""Best-effort warnings that must never change the hook's decision.

Every diagnostic the hook emits goes through :func:`warn`. Logging is an
observation aid, so a missing, closed, or broken standard error must cost a
warning at most. Writing through ``print`` without a stream is unsafe:
``file=None`` means standard output, which carries the JSON decision.
"""

from __future__ import annotations

import sys


def warn(message: str) -> None:
    """Write a fixed warning to standard error without risking the decision.

    Parameters
    ----------
    message
        Fixed text describing the failure. Callers never pass exception text,
        which could carry filesystem paths or other personal data.

    """
    stream = sys.stderr
    if stream is None:
        # A process started with file descriptor 2 closed has no standard
        # error. ``print`` would then write to standard output, which the hook
        # reserves for the JSON decision.
        return
    try:
        print(f"post-turn-quality-stop-hook: {message}", file=stream)
    except OSError, ValueError:
        # A closed or broken stream cannot change the decision, and the
        # exception must not escape into the hook's result.
        return
