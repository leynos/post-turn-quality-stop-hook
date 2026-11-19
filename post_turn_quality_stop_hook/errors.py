"""Shared fallibility type for stop-hook helpers.

Helpers that make a policy decision report failure with :class:`HookError`
instead of returning ad-hoc tuples. Keeping the type free of package imports
lets every layer depend on it without creating an import cycle.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(slots=True, frozen=True)
class HookError:
    """A failure reported by a policy-bearing hook helper.

    Attributes
    ----------
    operation
        Short identifier for the operation that failed.
    message
        Human-readable failure message.

    """

    operation: str
    message: str

    def __str__(self) -> str:
        """Return the human-readable failure message."""
        return self.message
