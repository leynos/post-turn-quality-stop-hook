"""Collected Git facts used by stop-hook gates."""

from __future__ import annotations

import dataclasses
import typing as typ

from post_turn_quality_stop_hook.git import (
    get_upstream_ref,
    is_three_way_merge_configured,
    merge_base,
    primary_remote_name,
    verify_ref,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.config import Config


@dataclasses.dataclass(slots=True, frozen=True)
class GitFacts:
    """Read-only facts about the repository and branch state."""

    primary_remote: str | None
    upstream_ref: str | None
    pr_base_local_ref: str | None
    local_base_commit: str | None
    three_way_merge_is_configured: bool


def collect_git_facts(
    repo: Path, config: Config, *, pr_base_branch: str | None = None
) -> GitFacts:
    """Collect best-effort Git facts without blocking on missing optional data."""
    primary_remote, _remote_err = primary_remote_name(repo, config.primary_remote)
    upstream_ref, _upstream_err = get_upstream_ref(repo)
    pr_base_local_ref = _local_ref(primary_remote, pr_base_branch)
    local_base_ref = (
        pr_base_local_ref
        or upstream_ref
        or _default_base_ref(repo, primary_remote, config.base_branch_default)
    )
    local_base_commit = _merge_base_or_none(repo, local_base_ref)
    return GitFacts(
        primary_remote=primary_remote,
        upstream_ref=upstream_ref,
        pr_base_local_ref=pr_base_local_ref,
        local_base_commit=local_base_commit,
        three_way_merge_is_configured=is_three_way_merge_configured(repo),
    )


def _local_ref(primary_remote: str | None, branch: str | None) -> str | None:
    if primary_remote is None or branch is None:
        return None
    return f"{primary_remote}/{branch}"


def _default_base_ref(
    repo: Path, primary_remote: str | None, branch: str
) -> str | None:
    if primary_remote is None:
        return None
    candidate = f"{primary_remote}/{branch}"
    ok, _err = verify_ref(repo, candidate)
    return candidate if ok else None


def _merge_base_or_none(repo: Path, ref: str | None) -> str | None:
    if ref is None:
        return None
    commit, _err = merge_base(repo, ref)
    return commit
