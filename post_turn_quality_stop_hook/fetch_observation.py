"""Report an actual ``git fetch`` to the pipeline observer.

Three fetches happen behind gates: the base ref that preparation ensures, the
pull-request base that the rebase check refreshes, and the upstream head that
the unpushed gate compares against. Each is a blocking command, so each is
reported through :class:`ObserverFetchObserver`, which binds the Git
fetch-observer protocol to the pipeline's observation seam. A fetch that does
not run reports nothing, so the durable record never invents command history.

The adapter is a decision-log concern: the Git helpers accept any observer, and
the pipeline supplies a no-op one when logging is disabled.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook.observer import CommandToken, PipelineObserver


class ObserverFetchObserver:
    """Report an actual ``git fetch`` to the pipeline observer.

    ``git fetch`` runs only when a ref is missing or a fetch is forced, so the
    command records are emitted from the observer rather than around the whole
    ``ensure_base_ref`` step. The exit code is the fetch process's own, which
    keeps a later merge-base failure from looking like a failed fetch.
    """

    def __init__(self, observer: PipelineObserver) -> None:
        """Store the observer that receives the fetch events."""
        self._observer = observer

    def fetch_started(self) -> CommandToken | None:
        """Start a command record before the blocking fetch."""
        return self._observer.command_started("fetch", "git fetch", [])

    def fetch_finished(self, token: CommandToken | None, exit_code: int) -> None:
        """Finish the record with the fetch's real exit code."""
        self._observer.command_finished(token, exit_code)
