"""Fetch a configured upstream branch into a private ref.

The unpushed-commits gate must compare ``HEAD`` with the remote's actual head,
not with the local remote-tracking cache. This module owns that network fetch
and the temporary ref that holds its result, keeping the transport work and
its cleanup out of the decision logic.
"""

from __future__ import annotations

import contextlib
import dataclasses
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import typing as typ
import uuid

from post_turn_quality_stop_hook.process import run
from post_turn_quality_stop_hook.redaction import redact_credentials, redact_remote

if typ.TYPE_CHECKING:
    from pathlib import Path

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
