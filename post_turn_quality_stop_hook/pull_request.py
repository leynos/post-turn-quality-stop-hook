"""Domain-facing value for the open pull request of a branch.

The pull-request rebase gate decides from these fields only, so this module
must stay free of transport and GitHub concerns. :mod:`.github` imports the
value and implements the lookup; the gate receives the lookup as a callable
and never imports the adapter.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(slots=True, frozen=True)
class PullRequestSummary:
    """Summary of the open pull request for the current branch.

    Attributes
    ----------
    number
        Pull request number.
    base_branch
        Target branch name for the pull request.
    base_oid
        Base branch commit OID reported by GitHub.

    """

    number: int
    base_branch: str
    base_oid: str
