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

    ``label`` is the display name for the blocking payload. ``ahead`` is
    ``True`` only when ``HEAD`` has commits absent from the refreshed upstream;
    it is ``None`` with an ``error`` when the comparison could not be made.
    """

    label: str
    ahead: bool | None
    error: str | None = None


class UpstreamHeadSource(typ.Protocol):
    """Port supplying the refreshed upstream comparison for a branch.

    The gate decides only from the value this port returns, so a test can
    supply a prepared comparison without Git or the network.
    """

    def comparison(self) -> UpstreamComparison | None:
        """Return the refreshed comparison, or ``None`` without an upstream."""
        ...
