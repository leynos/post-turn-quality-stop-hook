"""Display rule for a prepared upstream comparison.

The unpushed gate names its upstream in a warning, and the payload emitter
names it in a block, so the rule lives in its own domain-facing module. The
gate imports this instead of the formatter, and ``formatting`` re-exports the
same function for the payload it renders.

A configured remote name reads naturally as ``origin/feature``. A URL or
filesystem path does not, so it is shown as ``feature on <url>`` with its
userinfo removed. A remote that cannot be rendered safely, and a local upstream
branch, are shown by branch name alone so no part of a credential-bearing URL
can reach a payload.
"""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook.redaction import redact_remote_safely

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook.upstream_port import UpstreamComparison


def upstream_label(comparison: UpstreamComparison) -> str:
    """Render the upstream target for a blocking message and its warnings."""
    if comparison.remote == ".":
        return comparison.branch
    names = comparison.remote_names
    if names is not None and comparison.remote in names:
        return f"{comparison.remote}/{comparison.branch}"
    safe_remote = redact_remote_safely(comparison.remote)
    if safe_remote is None:
        return comparison.branch
    return f"{comparison.branch} on {safe_remote}"
