"""Port for observing an actual ``git fetch`` invocation.

The callbacks run only when a fetch really starts, so a decision log can record
the command without claiming one that never happened. The token type belongs to
the caller: ``fetch_started`` returns its own progress handle, and
``fetch_finished`` receives that same handle, so neither side needs a cast or an
opaque ``object``.

This module declares the port only. The Git adapter in :mod:`.git` accepts any
implementation, and :class:`ObserverFetchReporter` binds the pipeline's
observation seam to it.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook.observer import CommandToken


class FetchObserver[TokenT](typ.Protocol):
    """Observe the boundaries of an actual ``git fetch`` invocation."""

    def fetch_started(self) -> TokenT:
        """Record that a fetch is about to run and return a progress token."""
        ...

    def fetch_finished(self, token: TokenT, exit_code: int) -> None:
        """Record the fetch's exit code against the token it returned."""
        ...


class FetchReporter(typ.Protocol):
    """A fetch observer reporting the pipeline's command token.

    Branch-state operations that fetch take this narrower form, so a domain
    port does not need a generic parameter merely to name the observer.
    """

    def fetch_started(self) -> CommandToken | None:
        """Start a command record before the blocking fetch."""
        ...

    def fetch_finished(self, token: CommandToken | None, exit_code: int) -> None:
        """Finish the record with the fetch's real exit code."""
        ...
