"""Domain-facing types for comparing a branch with its upstream.

The unpushed-commits gate decides only from these types, so this module must
stay free of transport and Git concerns. The Git adapter in `upstream` imports
them and implements `UpstreamHeadSource`; the pipeline imports them and never
the adapter, which keeps the decision path independent of how the upstream head
is obtained.
"""

from __future__ import annotations

import dataclasses
import typing as typ


@dataclasses.dataclass(slots=True, frozen=True)
class UpstreamComparison:
    """State the unpushed decision compares against.

    ``remote`` is the configured upstream remote name or URL, ``branch`` its
    short branch name, and ``remote_names`` the configured remote names read
    for display purposes (``None`` when that read failed). The decision path
    renders a label from these fields, so no presentation rule lives in the
    transport adapter.

    ``ahead`` is ``True`` only when ``HEAD`` has commits absent from the
    refreshed upstream; it is ``None`` with an ``error`` when the comparison
    could not be made.
    """

    remote: str
    branch: str
    remote_names: tuple[str, ...] | None = None
    ahead: bool | None = None
    error: str | None = None


class UpstreamHeadSource(typ.Protocol):
    """Port supplying the refreshed upstream comparison for a branch.

    Calling the port performs network and filesystem side effects: it fetches
    the upstream head, writes a temporary private ref, reads that ref, and
    deletes it again. The gate decides only from the value the call returns,
    so a test can supply a prepared comparison without Git or the network.
    """

    def refresh_comparison(self) -> UpstreamComparison | None:
        """Fetch the upstream and compare it with HEAD; ``None`` if unset."""
        ...
