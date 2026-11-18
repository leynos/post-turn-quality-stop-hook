"""Resolve and refresh the upstream a local branch is compared with.

The unpushed-commits gate must compare ``HEAD`` with the remote's actual head,
not with the local remote-tracking cache. This module owns that work: the
network fetch, the temporary ref that holds its result, the display label,
and the comparison itself. It implements the `UpstreamHeadSource` port from
`upstream_port`, so the gate decides from a value object and never performs
transport work.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import typing as typ
import uuid

from post_turn_quality_stop_hook.git import has_unpushed_commits, remote_names
from post_turn_quality_stop_hook.process import run
from post_turn_quality_stop_hook.redaction import redact_credentials, redact_remote
from post_turn_quality_stop_hook.upstream_port import UpstreamComparison

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.config import Config
    from post_turn_quality_stop_hook.git_facts import GitFacts

logger = logging.getLogger(__name__)

#: A command runner returning a completed process. Tests inject one to prove
#: the fetch and cleanup sequence without invoking Git.
CommandRunner = typ.Callable[..., subprocess.CompletedProcess[str]]

#: A factory for the private ref that receives the fetched head. Injecting it
#: keeps the random source out of the fetch function and makes the ref name
#: predictable in tests.
TempRefFactory = typ.Callable[[], str]

#: Prefix for the private refs used to hold a fetched upstream head. A unique
#: ref per fetch isolates the result from any concurrent fetch, which would
#: otherwise overwrite the shared ``FETCH_HEAD``.
_TEMP_FETCH_REF_PREFIX = "refs/post-turn-quality/fetch"

#: Seconds allowed for deleting the temporary fetch ref. Deleting a ref runs
#: the repository's ``reference-transaction`` hook, so a hung hook must not be
#: able to stall the hook after the fetch itself has been bounded.
_REF_CLEANUP_TIMEOUT_SECONDS = 5.0


def _new_temp_ref() -> str:
    """Return a fresh private ref name for a fetched upstream head."""
    return f"{_TEMP_FETCH_REF_PREFIX}/{uuid.uuid4().hex}"


@dataclasses.dataclass(slots=True, frozen=True)
class FetchOptions:
    """Optional controls for one upstream fetch.

    ``timeout`` bounds the fetch command. ``runner`` and ``temp_ref_factory``
    are injection seams: tests supply them to drive the fetch and cleanup
    sequence without Git or the network.
    """

    timeout: float | None = None
    runner: CommandRunner | None = None
    temp_ref_factory: TempRefFactory = _new_temp_ref


def _qualify_upstream_ref(upstream_ref: str) -> str:
    """Return a fully qualified ref for a configured upstream.

    ``branch.<name>.merge`` normally stores ``refs/heads/<branch>``. Callers
    that pass the short branch name are qualified the same way, so the fetch
    can never resolve a same-named tag instead.
    """
    if upstream_ref.startswith("refs/"):
        return upstream_ref
    return f"refs/heads/{upstream_ref}"


def _delete_temp_ref(
    repo: Path, temp_ref: str, *, runner: CommandRunner | None = None
) -> None:
    """Delete a private fetch ref, ignoring a missing ref or cleanup failure."""
    command = runner or run
    with contextlib.suppress(OSError):
        command(
            ["git", "update-ref", "-d", temp_ref],
            repo,
            timeout=_REF_CLEANUP_TIMEOUT_SECONDS,
        )


def fetch_upstream_head(
    repo: Path,
    remote: str,
    upstream_ref: str,
    *,
    options: FetchOptions | None = None,
) -> tuple[str | None, str | None]:
    """Fetch an upstream branch and return the remote head commit.

    ``remote`` may be a configured remote name or a URL. The branch is fetched
    into a unique private ref, so its result cannot be replaced by another
    fetch between the fetch and the read. This also makes the result
    authoritative when an explicit-URL push left the local remote-tracking ref
    stale. Any credentials in ``remote`` are removed from the returned error,
    which may otherwise be logged or shown to the user.

    Parameters
    ----------
    repo
        Repository path.
    remote
        Remote name or URL to fetch from.
    upstream_ref
        Fully qualified upstream ref to fetch, normally
        ``refs/heads/<branch>``. A short branch name is qualified as a branch.
    options
        Timeout and injection seams for the fetch. The module-level runner and
        the default ref factory are used when omitted.

    Returns
    -------
    tuple[str | None, str | None]
        Fetched head commit and no error on success, or ``(None, error)``.

    """
    settings = options or FetchOptions()
    command = settings.runner or run
    display_remote = redact_remote(remote)
    source_ref = _qualify_upstream_ref(upstream_ref)
    display_ref = source_ref.removeprefix("refs/heads/")
    temp_ref = settings.temp_ref_factory()
    try:
        # ``--`` keeps a remote URL or refspec from being read as an option.
        # The explicit fully qualified source ref avoids resolving a tag that
        # shares the branch name, and ``--no-write-fetch-head`` keeps the
        # shared FETCH_HEAD untouched.
        fetch = command(
            [
                "git",
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "--",
                remote,
                f"+{source_ref}:{temp_ref}",
            ],
            repo,
            timeout=settings.timeout,
        )
        if fetch.returncode != 0:
            # Git may echo the configured URL verbatim or in a shortened form.
            error_output = redact_credentials(
                fetch.stderr.strip() or fetch.stdout.strip()
            )
            return None, (
                f"git fetch {display_remote} {display_ref} failed: {error_output}"
            )

        resolved = command(
            ["git", "rev-parse", "--verify", f"{temp_ref}^{{commit}}"], repo
        )
        if resolved.returncode != 0:
            error_output = resolved.stderr.strip() or resolved.stdout.strip()
            return None, f"git rev-parse {display_ref} failed: {error_output}"
        head = resolved.stdout.strip()
        if not head:
            return None, f"git rev-parse {display_ref} returned empty output"
        return head, None
    finally:
        _delete_temp_ref(repo, temp_ref, runner=command)


@dataclasses.dataclass(slots=True, frozen=True)
class _UpstreamTarget:
    """The configured upstream a local branch should be compared against.

    ``merge_ref`` stays fully qualified because a short branch name is
    ambiguous when the remote also has a tag with that name. ``branch`` is the
    short display name, and ``local_ref`` is the remote-tracking ref to fall
    back to when upstream refresh is disabled.
    """

    remote: str
    merge_ref: str
    branch: str
    local_ref: str | None


def _configured_upstream(facts: GitFacts) -> _UpstreamTarget | None:
    """Return the upstream collected in ``GitFacts``, if one is configured.

    ``collect_git_facts`` always populates the upstream fields, so this never
    re-reads Git configuration. ``None`` means there is no upstream to compare
    against.
    """
    # Separate checks rather than one boolean chain: each early return narrows
    # one field for the type checker, which a combined test would not do.
    if facts.upstream_remote is None:
        return None
    if facts.upstream_merge_ref is None:
        return None
    if facts.upstream_branch is None:
        return None
    return _UpstreamTarget(
        facts.upstream_remote,
        facts.upstream_merge_ref,
        facts.upstream_branch,
        facts.upstream_ref,
    )


def _upstream_label(remote: str, branch: str, names: tuple[str, ...] | None) -> str:
    """Render the upstream target for the blocking message.

    A configured remote name reads naturally as ``origin/feature``. A URL or
    filesystem path does not, so it is shown as ``feature on <url>``. A local
    upstream branch is shown by its own name. URL userinfo is removed so a
    tokenised remote never reaches the block payload.

    ``names`` is the already-read remote list, or ``None`` when that read
    failed. This helper performs no I/O: the adapter reads the list once.
    """
    if remote == ".":
        return branch
    if names is not None and remote in names:
        return f"{remote}/{branch}"
    return f"{branch} on {redact_remote(remote)}"


@dataclasses.dataclass(slots=True)
class GitUpstreamHeadSource:
    """Git adapter that refreshes the configured upstream and compares HEAD.

    This is the production `UpstreamHeadSource`. It reads remote names once,
    classifies the upstream as a remote name, URL, or local ``"."`` branch,
    refreshes the head unless the opt-out is set, and compares it with HEAD.
    """

    repo: Path
    facts: GitFacts | None
    config: Config

    def comparison(self) -> UpstreamComparison | None:
        """Return the refreshed upstream comparison, if one is configured."""
        if self.facts is None:
            return None
        target = _configured_upstream(self.facts)
        if target is None:
            return None
        label = _upstream_label(target.remote, target.branch, self._remote_names())
        head, err = self._head_for(target)
        if head is None:
            return UpstreamComparison(label=label, ahead=None, error=err)
        ahead, err = has_unpushed_commits(self.repo, head)
        return UpstreamComparison(label=label, ahead=ahead, error=err)

    def _remote_names(self) -> tuple[str, ...] | None:
        """Read remote names once, returning ``None`` when the read fails."""
        names, err = remote_names(self.repo)
        if err is not None:
            logger.warning(
                "unpushed gate could not read remote names; using a URL label: %s",
                err,
            )
            return None
        return tuple(names) if names is not None else None

    def _head_for(self, target: _UpstreamTarget) -> tuple[str | None, str | None]:
        """Return the commit ``HEAD`` should be compared against.

        A ``"."`` remote names a local branch, which is already authoritative
        and never needs a fetch. The configured opt-out falls back to the local
        remote-tracking ref, which can be stale after an explicit-URL push.
        """
        if target.remote == ".":
            return target.merge_ref, None
        if self.config.unpushed_commits_refresh_upstream:
            return fetch_upstream_head(
                self.repo,
                target.remote,
                target.merge_ref,
                options=FetchOptions(
                    timeout=self.config.unpushed_commits_fetch_timeout_seconds
                ),
            )
        if target.local_ref is None:
            return (
                None,
                "upstream refresh is disabled and the upstream is not a local ref",
            )
        return target.local_ref, None
