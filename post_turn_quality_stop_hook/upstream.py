"""Resolve and refresh the upstream a local branch is compared with.

The unpushed-commits gate must compare ``HEAD`` with the remote's actual head,
not with the local remote-tracking cache. This module owns that work: the
network fetch, the temporary ref that holds its result, and the comparison
itself. It implements the `UpstreamHeadSource` port from `upstream_port`, so
the gate decides from a value object and never performs transport work. Block
labels are rendered by the pipeline from the comparison's fields.
"""

from __future__ import annotations

import dataclasses
import logging
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import typing as typ
import uuid

from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.git import (
    FETCH_LAUNCH_FAILURE_STATUS,
    FetchObserver,
    current_branch,
    has_unpushed_commits,
    remote_names,
)
from post_turn_quality_stop_hook.process import run
from post_turn_quality_stop_hook.redaction import (
    redact_remote_safely,
    redact_text_safely,
)
from post_turn_quality_stop_hook.upstream_port import UpstreamComparison

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.git_facts import GitFacts
    from post_turn_quality_stop_hook.observer import CommandToken

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
    sequence without Git or the network. ``observer``, when supplied, is
    notified immediately before and after the fetch itself, so the run's
    decision log can record the blocking command with its own exit code. The
    private ref's cleanup is not a fetched branch and is not reported.
    """

    timeout: float | None = None
    runner: CommandRunner | None = None
    temp_ref_factory: TempRefFactory = _new_temp_ref
    observer: FetchObserver[CommandToken | None] | None = None


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
    """Delete a private fetch ref, reporting a failure without raising.

    Deletion is best effort: it runs the repository's
    ``reference-transaction`` hook, which can reject it, and a leftover private
    ref only keeps an otherwise unreachable history alive. A failure is
    therefore logged so the ref can be cleaned up by hand, and never
    invalidates the comparison the caller already obtained.
    """
    command = runner or run
    try:
        deleted = command(
            ["git", "update-ref", "-d", temp_ref],
            repo,
            timeout=_REF_CLEANUP_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        logger.warning(
            "unpushed gate could not delete the temporary fetch ref %s: %s",
            temp_ref,
            exc,
        )
        return
    if deleted.returncode != 0:
        # The ref name is ours and safe to log. Git's own diagnostics are not
        # printed verbatim: a hook it ran may have repeated a credential.
        detail = redact_text_safely(deleted.stderr.strip())
        logger.warning(
            "unpushed gate could not delete the temporary fetch ref %s: %s",
            temp_ref,
            detail if detail is not None else "the error could not be shown safely",
        )


def _fetch_failure(
    display_remote: str | None,
    display_ref: str,
    fetch: subprocess.CompletedProcess[str],
) -> HookError:
    """Describe a failed fetch without echoing a credential-bearing remote.

    Git can repeat the configured URL, shortened or verbatim, in its
    diagnostics. When no safe rendering of the remote exists, both the remote
    and that output are withheld: the message names only the branch. The same
    holds for a named remote whose configured URL is malformed: the remote
    name is safe, but the diagnostics it produced are not.
    """
    if display_remote is None:
        return HookError(
            "fetch_upstream_head",
            f"git fetch of {display_ref} from the configured upstream failed",
        )
    error_output = redact_text_safely(fetch.stderr.strip() or fetch.stdout.strip())
    if error_output is None:
        return HookError(
            "fetch_upstream_head",
            f"git fetch {display_remote} {display_ref} failed: "
            "the error output could not be shown safely",
        )
    return HookError(
        "fetch_upstream_head",
        f"git fetch {display_remote} {display_ref} failed: {error_output}",
    )


def _launch_failure(
    display_remote: str | None, display_ref: str, exc: OSError
) -> HookError:
    """Describe a fetch whose transport could not be started.

    The exception text comes from the runner, so it is redacted like any other
    diagnostic and withheld when a credential could remain.
    """
    detail = redact_text_safely(str(exc)) or "the error could not be shown safely"
    if display_remote is None:
        return HookError(
            "fetch_upstream_head",
            f"git fetch of {display_ref} from the configured upstream "
            f"could not run: {detail}",
        )
    return HookError(
        "fetch_upstream_head",
        f"git fetch {display_remote} {display_ref} could not run: {detail}",
    )


def _observed_fetch(
    command: CommandRunner,
    args: list[str],
    repo: Path,
    settings: FetchOptions,
) -> subprocess.CompletedProcess[str]:
    """Run the fetch, reporting its start and exit status to the observer.

    The token and the observer that issued it stay together, so a token is
    never paired with an absent observer. A runner that cannot start the
    transport raises ``OSError`` instead of returning a result. The record is
    still finished, so it never claims a command that started and never ended,
    but with the documented synthetic status rather than one a real ``git
    fetch`` could report. The exception is re-raised for the caller's
    launch-failure handling.
    """
    observed = settings.observer
    token = observed.fetch_started() if observed is not None else None
    try:
        fetch = command(args, repo, timeout=settings.timeout)
    except OSError:
        if observed is not None:
            observed.fetch_finished(token, FETCH_LAUNCH_FAILURE_STATUS)
        raise
    if observed is not None:
        observed.fetch_finished(token, fetch.returncode)
    return fetch


def fetch_upstream_head(
    repo: Path,
    remote: str,
    upstream_ref: str,
    *,
    options: FetchOptions | None = None,
) -> tuple[str | None, HookError | None]:
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
    tuple[str | None, HookError | None]
        Fetched head commit and no error on success, or ``(None, error)``.

    """
    settings = options or FetchOptions()
    command = settings.runner or run
    display_remote = redact_remote_safely(remote)
    source_ref = _qualify_upstream_ref(upstream_ref)
    display_ref = source_ref.removeprefix("refs/heads/")
    temp_ref = settings.temp_ref_factory()
    try:
        # ``--`` keeps a remote URL or refspec from being read as an option.
        # The explicit fully qualified source ref avoids resolving a tag that
        # shares the branch name. ``--no-write-fetch-head`` leaves the shared
        # FETCH_HEAD alone, and ``--refmap=`` clears the remote's configured
        # refspec mapping so this fetch cannot move a remote-tracking ref and
        # silently change what a later ``--force-with-lease`` expects.
        # ``--no-recurse-submodules`` keeps the result dependent only on the
        # superproject head this gate compares.
        fetch = _observed_fetch(
            command,
            [
                "git",
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "--no-recurse-submodules",
                "--refmap=",
                "--",
                remote,
                f"+{source_ref}:{temp_ref}",
            ],
            repo,
            settings,
        )
        if fetch.returncode != 0:
            return None, _fetch_failure(display_remote, display_ref, fetch)

        resolved = command(
            ["git", "rev-parse", "--verify", f"{temp_ref}^{{commit}}"], repo
        )
        if resolved.returncode != 0:
            error_output = resolved.stderr.strip() or resolved.stdout.strip()
            return None, HookError(
                "fetch_upstream_head",
                f"git rev-parse {display_ref} failed: {error_output}",
            )
    except OSError as exc:
        # A runner that cannot start the transport raises instead of returning
        # a result. Report it in the same shape as any other fetch failure so
        # the gate fails open, and keep the temp-ref cleanup in ``finally``.
        return None, _launch_failure(display_remote, display_ref, exc)
    else:
        head = resolved.stdout.strip()
        if not head:
            return None, HookError(
                "fetch_upstream_head",
                f"git rev-parse {display_ref} returned empty output",
            )
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


def _comparison_for(
    target: _UpstreamTarget,
    names: tuple[str, ...] | None,
    *,
    ahead: bool | None = None,
    error: HookError | None = None,
) -> UpstreamComparison:
    """Build the port value for one target without rendering a label."""
    return UpstreamComparison(
        remote=target.remote,
        branch=target.branch,
        remote_names=names,
        ahead=ahead,
        error=error,
    )


@dataclasses.dataclass(slots=True, frozen=True)
class _Checkout:
    """The local branch and HEAD commit seen at one moment."""

    branch: str | None
    head: str


def _checkout_state(repo: Path) -> tuple[_Checkout | None, HookError | None]:
    """Read the current branch and HEAD commit, or report why that failed."""
    branch, branch_err = current_branch(repo)
    if branch_err is not None:
        return None, branch_err
    resolved = run(["git", "rev-parse", "--verify", "HEAD^{commit}"], repo)
    if resolved.returncode != 0:
        error_output = resolved.stderr.strip() or resolved.stdout.strip()
        return None, HookError(
            "current_branch", f"git rev-parse HEAD failed: {error_output}"
        )
    commit = resolved.stdout.strip()
    if not commit:
        return None, HookError(
            "current_branch", "git rev-parse HEAD returned empty output"
        )
    return _Checkout(branch, commit), None


@dataclasses.dataclass(slots=True)
class GitUpstreamHeadSource:
    """Git adapter that refreshes the configured upstream and compares HEAD.

    This is the production `UpstreamHeadSource`. It reads remote names once,
    classifies the upstream as a remote name, URL, or local ``"."`` branch,
    refreshes the head unless the opt-out is set, and compares it with HEAD.
    The two settings it needs are passed as values rather than as a
    configuration object, so its side effects depend only on them.
    """

    repo: Path
    facts: GitFacts | None
    refresh_upstream: bool
    fetch_timeout_seconds: float
    observer: FetchObserver[CommandToken | None] | None = None

    def refresh_comparison(self) -> UpstreamComparison | None:
        """Fetch the upstream head and compare it with the checked-out HEAD."""
        if self.facts is None:
            return None
        target = _configured_upstream(self.facts)
        if target is None:
            return None
        names = self._remote_names()
        before, err = self._checkout_matching(self.facts)
        if before is None:
            return _comparison_for(target, names, error=err)
        head, fetch_err = self._head_for(target)
        after, after_err = _checkout_state(self.repo)
        if after_err is None and after != before:
            return _comparison_for(
                target,
                names,
                error=HookError(
                    "refresh_comparison",
                    "the checkout changed while the upstream head was fetched",
                ),
            )
        if head is None:
            return _comparison_for(target, names, error=fetch_err)
        ahead, err = has_unpushed_commits(self.repo, head)
        return _comparison_for(target, names, ahead=ahead, error=err)

    def _checkout_matching(
        self, facts: GitFacts
    ) -> tuple[_Checkout | None, HookError | None]:
        """Return the checkout state, or an error when it left the snapshot.

        The upstream belongs to the branch the facts were collected for. If
        the checkout has moved since, comparing this HEAD with that upstream
        would count another branch's commits, so the gate must not block.
        """
        state, err = _checkout_state(self.repo)
        if state is None:
            return None, err
        if facts.branch is None or state.branch == facts.branch:
            return state, None
        moved_to = state.branch or "a detached HEAD"
        return None, HookError(
            "current_branch",
            f"the checkout moved from {facts.branch} to {moved_to} "
            "during the unpushed check",
        )

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

    def _head_for(self, target: _UpstreamTarget) -> tuple[str | None, HookError | None]:
        """Return the commit ``HEAD`` should be compared against.

        A ``"."`` remote names a local branch, which is already authoritative
        and never needs a fetch. The configured opt-out falls back to the local
        remote-tracking ref, which can be stale after an explicit-URL push.
        """
        if target.remote == ".":
            return target.merge_ref, None
        if self.refresh_upstream:
            return fetch_upstream_head(
                self.repo,
                target.remote,
                target.merge_ref,
                options=FetchOptions(
                    timeout=self.fetch_timeout_seconds,
                    observer=self.observer,
                ),
            )
        if target.local_ref is None:
            return (
                None,
                HookError(
                    "refresh_comparison",
                    "upstream refresh is disabled and the upstream is not a local ref",
                ),
            )
        return target.local_ref, None
