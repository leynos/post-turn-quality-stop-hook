"""Collected Git facts used by stop-hook gates."""

from __future__ import annotations

import dataclasses
import typing as typ

from post_turn_quality_stop_hook.git import (
    get_upstream_ref,
    is_three_way_merge_configured,
    merge_base,
    primary_remote_name,
    resolve_upstream,
    verify_ref,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.config import Config


@dataclasses.dataclass(slots=True, frozen=True)
class GitFacts:
    """Read-only facts about the repository and branch state.

    Attributes
    ----------
    primary_remote
        Remote name selected as authoritative, or ``None`` when unavailable.
    upstream_ref
        Local remote-tracking ref for the current branch's upstream, or
        ``None``. This ref is a local cache and may not match the remote.
    pr_base_local_ref
        Local remote-tracking ref for the PR base branch, or ``None``.
    local_base_commit
        Merge-base commit used by branch-state checks, or ``None``.
    three_way_merge_is_configured
        Whether Git conflict style is configured for three-way markers.
    upstream_remote
        Configured upstream remote name or URL, or ``None``.
    upstream_branch
        Configured upstream branch name, or ``None``.
    upstream_merge_ref
        Fully qualified configured upstream ref, or ``None``. This is the ref
        to fetch; ``upstream_branch`` is its short display name.
    upstream_error
        Error encountered while resolving the upstream, or ``None``.

    """

    primary_remote: str | None
    upstream_ref: str | None
    pr_base_local_ref: str | None
    local_base_commit: str | None
    three_way_merge_is_configured: bool
    upstream_remote: str | None = None
    upstream_branch: str | None = None
    upstream_merge_ref: str | None = None
    upstream_error: str | None = None


def collect_git_facts(
    repo: Path, config: Config, *, pr_base_branch: str | None = None
) -> GitFacts:
    """Collect best-effort Git facts without blocking on optional data.

    Parameters
    ----------
    repo
        Repository path used for Git plumbing commands.
    config
        Merged stop-hook configuration used for remote and base defaults.
    pr_base_branch
        Optional pull request base branch. When omitted, PR-local ref fields
        are left unavailable and the upstream or configured default base is
        used when present.

    Returns
    -------
    GitFacts
        Collected repository facts. Optional fields such as
        ``primary_remote``, ``upstream_ref``, ``pr_base_local_ref``, and
        ``local_base_commit`` may be ``None`` when the corresponding data is
        unavailable. Missing optional data does not raise.

    """
    primary_remote, _remote_err = primary_remote_name(repo, config.primary_remote)
    upstream_ref, _upstream_err = get_upstream_ref(repo)
    upstream = _upstream_facts(repo)
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
        upstream_remote=upstream.remote,
        upstream_branch=upstream.branch,
        upstream_merge_ref=upstream.merge_ref,
        upstream_error=upstream.error,
    )


@dataclasses.dataclass(slots=True, frozen=True)
class _UpstreamFacts:
    """Configured upstream details resolved from Git configuration."""

    remote: str | None
    merge_ref: str | None
    branch: str | None
    error: str | None


def _upstream_facts(repo: Path) -> _UpstreamFacts:
    """Resolve the configured upstream and its short display name."""
    remote, merge_ref, error = resolve_upstream(repo)
    return _UpstreamFacts(
        remote=remote,
        merge_ref=merge_ref,
        branch=_short_branch_name(merge_ref),
        error=error,
    )


def _short_branch_name(upstream_ref: str | None) -> str | None:
    """Return the display name for a fully qualified upstream ref."""
    if upstream_ref is None:
        return None
    return upstream_ref.removeprefix("refs/heads/")


def _local_ref(primary_remote: str | None, branch: str | None) -> str | None:
    """Return the remote-tracking ref for branch, or None if either part is missing."""
    if primary_remote is None or branch is None:
        return None
    return f"{primary_remote}/{branch}"


def _default_base_ref(
    repo: Path, primary_remote: str | None, branch: str
) -> str | None:
    """Return the remote-tracking default base ref when Git can verify it.

    Return None when there is no primary remote or the ref does not resolve.
    """
    if primary_remote is None:
        return None
    candidate = f"{primary_remote}/{branch}"
    ok, _err = verify_ref(repo, candidate)
    return candidate if ok else None


def _merge_base_or_none(repo: Path, ref: str | None) -> str | None:
    """Return the merge base of ref and HEAD, or None when it is unavailable."""
    if ref is None:
        return None
    commit, _err = merge_base(repo, ref)
    return commit
