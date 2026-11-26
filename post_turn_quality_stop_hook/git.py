"""Git plumbing layer: queries, configuration reads, and ref operations.

Provides repository resolution, fetching, ref manipulation, merge-base,
changed-file enumeration, working-tree queries, and upstream configuration
reads. Subprocess execution is delegated to `process.py`, so this module holds
no supervision or signalling logic.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.process import (
    LAUNCH_FAILURE_STATUS,
    LaunchedProcess,
    run,
)

if typ.TYPE_CHECKING:
    import subprocess

#: Exit code recorded for a fetch whose transport could not be started. It is
#: the shared "no child answered" value, kept under its fetch-specific name for
#: the fetch observer that reports it.
FETCH_LAUNCH_FAILURE_STATUS = LAUNCH_FAILURE_STATUS


class FetchObserver[TokenT](typ.Protocol):
    """Observe the boundaries of an actual ``git fetch`` invocation.

    The callbacks run only when a fetch really starts, so a decision log can
    record the command without claiming one that never happened. The token
    type belongs to the caller: ``fetch_started`` returns its own progress
    handle, and ``fetch_finished`` receives that same handle, so neither side
    needs a cast or an opaque ``object``.
    """

    def fetch_started(self) -> TokenT:
        """Record that a fetch is about to run and return a progress token."""
        ...

    def fetch_finished(self, token: TokenT, exit_code: int) -> None:
        """Record the fetch's exit code against the token it returned."""
        ...


def repo_root(start_cwd: Path) -> tuple[Path | None, HookError | None]:
    """Resolve the git repository root for a starting directory.

    Parameters
    ----------
    start_cwd
        Directory to resolve from.

    Returns
    -------
    tuple[Path | None, HookError | None]
        Repository root path and hook error, if any.

    """
    p = run(["git", "rev-parse", "--show-toplevel"], start_cwd)
    if p.returncode != 0:
        return None, HookError(
            "repo_root",
            p.stderr.strip() or p.stdout.strip() or "not a git repository",
        )
    root = p.stdout.strip()
    if not root:
        return None, HookError(
            "repo_root", "git rev-parse --show-toplevel returned empty output"
        )
    return Path(root), None


def remote_names(repo: Path) -> tuple[list[str] | None, HookError | None]:
    """Return configured Git remote names in deterministic order.

    Parameters
    ----------
    repo
        Repository path.

    Returns
    -------
    tuple[list[str] | None, HookError | None]
        Sorted remote names and no error on success. On failure, the names are
        ``None`` and the hook error contains Git stderr or stdout.

    """
    remotes = run(["git", "remote"], repo)
    if remotes.returncode != 0:
        return (
            None,
            HookError(
                "remote_names",
                f"git remote failed: "
                f"{remotes.stderr.strip() or remotes.stdout.strip()}",
            ),
        )
    return sorted(remotes.stdout.split()), None


def primary_remote_name(
    repo: Path, configured: str | None
) -> tuple[str | None, HookError | None]:
    """Resolve the primary remote from config, origin, or first remote.

    Parameters
    ----------
    repo
        Repository path.
    configured
        Configured remote name, or ``None`` for automatic selection.

    Returns
    -------
    tuple[str | None, HookError | None]
        Selected remote and hook error. Uses ``configured`` when present in
        remotes, otherwise prefers ``origin``, otherwise the first remote.
        Returns ``(None, error)`` when ``remote_names`` fails and ``(None,
        None)`` when no suitable remote is available.

    """
    remotes, err = remote_names(repo)
    if remotes is None:
        return None, err
    if configured:
        return (configured, None) if configured in remotes else (None, None)
    if "origin" in remotes:
        return "origin", None
    return (remotes[0], None) if remotes else (None, None)


def fetch_remote_branch[TokenT](
    repo: Path,
    remote: str,
    branch: str,
    *,
    observer: FetchObserver[TokenT] | None = None,
) -> tuple[bool, HookError | None]:
    """Fetch one branch from one remote.

    Parameters
    ----------
    repo
        Repository path.
    remote
        Remote name to fetch from.
    branch
        Branch name to fetch.
    observer
        Optional observer notified when the fetch actually runs. It receives
        the process's own exit code, including the synthetic status of a
        launch failure, so the decision log never attributes a later failure
        to a fetch that succeeded.

    Returns
    -------
    tuple[bool, HookError | None]
        ``ok`` is ``True`` on success. ``error_msg`` contains the hook error on failure.

    """
    token = _start_fetch(observer)
    fetch = run(["git", "fetch", "--quiet", remote, branch], repo)
    _finish_fetch(observer, token, fetch)
    if fetch.returncode != 0:
        error_output = fetch.stderr.strip() or fetch.stdout.strip()
        return False, HookError(
            "fetch_remote_branch",
            f"git fetch {remote} {branch} failed: {error_output}",
        )
    return True, None


def _start_fetch[TokenT](observer: FetchObserver[TokenT] | None) -> TokenT | None:
    """Notify an observer that a fetch is starting, or return no token."""
    return None if observer is None else observer.fetch_started()


def _finish_fetch[TokenT](
    observer: FetchObserver[TokenT] | None,
    token: TokenT | None,
    fetch: subprocess.CompletedProcess[str],
) -> None:
    """Report a completed fetch with the status the process actually returned."""
    if observer is None:
        # ``token`` can only be ``None`` when no observer was supplied, so this
        # branch and the one below agree about whether a record exists.
        return
    observer.fetch_finished(typ.cast("TokenT", token), _reported_status(fetch))


def _reported_status(fetch: subprocess.CompletedProcess[str]) -> int:
    """Return the fetch's status, or the synthetic launch-failure value.

    ``process.run`` answers a launch failure with status ``1``, which a real
    ``git fetch`` also uses. A record that named ``1`` would be indistinguishable
    from a fetch that ran and failed, so the documented synthetic value is
    reported instead.
    """
    if isinstance(fetch, LaunchedProcess) and not fetch.launched:
        return FETCH_LAUNCH_FAILURE_STATUS
    return fetch.returncode


def ref_exists(repo: Path, ref: str) -> tuple[bool, HookError | None]:
    """Check whether a ref exists.

    Parameters
    ----------
    repo
        Repository root path.
    ref
        Fully qualified ref name.

    Returns
    -------
    tuple[bool, HookError | None]
        True if the ref exists, otherwise False and an error if the check failed.

    """
    verify = run(["git", "show-ref", "--verify", "--quiet", ref], repo)
    if verify.returncode == 0:
        return True, None
    if verify.returncode == 1:
        return False, None
    return (
        False,
        HookError(
            "ref_exists",
            verify.stderr.strip() or verify.stdout.strip() or "git show-ref failed",
        ),
    )


def verify_ref(repo: Path, ref: str) -> tuple[bool, HookError | None]:
    """Verify that a ref can be resolved.

    Parameters
    ----------
    repo
        Repository root path.
    ref
        Ref name to verify with rev-parse.

    Returns
    -------
    tuple[bool, HookError | None]
        ok and hook error, if any.

    """
    rp = run(["git", "rev-parse", "--verify", "--quiet", ref], repo)
    if rp.returncode != 0:
        return False, HookError("verify_ref", f"Cannot resolve {ref}")
    return True, None


def ensure_remote_branch_ref[TokenT](  # ruff:ignore[too-many-arguments]  # repo, remote, branch, the fetch policy and the observer are distinct inputs.
    repo: Path,
    remote: str,
    branch: str,
    *,
    always_fetch: bool,
    observer: FetchObserver[TokenT] | None = None,
) -> tuple[bool, HookError | None, bool]:
    """Ensure ``refs/remotes/<remote>/<branch>`` exists, fetching if needed.

    Parameters
    ----------
    repo
        Repository path.
    remote
        Remote name whose tracking ref should exist.
    branch
        Branch name whose tracking ref should exist.
    always_fetch
        Whether to fetch before checking the ref.
    observer
        Optional observer notified only when a fetch actually runs.

    Returns
    -------
    tuple[bool, HookError | None, bool]
        ``ok``, optional hook error, and whether a fetch was attempted.

    Notes
    -----
    ``ensure_remote_branch_ref`` uses ``ref_exists`` to check the tracking ref
    and ``fetch_remote_branch`` when fetching is required.

    """
    fetched = always_fetch
    ref = f"refs/remotes/{remote}/{branch}"
    if not always_fetch:
        exists, err = ref_exists(repo, ref)
        if err:
            return False, err, fetched
        if exists:
            return True, None, fetched

    ok, err = fetch_remote_branch(repo, remote, branch, observer=observer)
    if not ok:
        return False, err, fetched
    fetched = True

    exists, err = ref_exists(repo, ref)
    if err:
        return False, err, fetched
    if not exists:
        return (
            False,
            HookError(
                "ensure_remote_branch_ref",
                f"{remote}/{branch} still missing after fetch",
            ),
            fetched,
        )
    return True, None, fetched


def ensure_base_ref[TokenT](  # ruff:ignore[too-many-arguments]  # repo, base ref, fetch policy, remote and observer are distinct inputs.
    repo: Path,
    base_ref: str,
    *,
    always_fetch: bool,
    primary_remote: str = "origin",
    observer: FetchObserver[TokenT] | None = None,
) -> tuple[bool, HookError | None, bool]:
    """Ensure a base ref is available and resolvable.

    Parameters
    ----------
    repo
        Repository root path.
    base_ref
        Base git ref used to compute the merge-base.
    always_fetch
        If True, always fetch origin/main when base_ref is origin/main.
    primary_remote
        Remote name used when the base ref is a remote-tracking branch.
    observer
        Optional observer notified only when a fetch actually runs.

    Returns
    -------
    tuple[bool, HookError | None, bool]
        ok, hook error (if any), fetched.

    """
    remote_prefix = f"{primary_remote}/"
    if base_ref.startswith(remote_prefix):
        branch = base_ref.removeprefix(remote_prefix)
        ok, err, fetched = ensure_remote_branch_ref(
            repo,
            primary_remote,
            branch,
            always_fetch=always_fetch,
            observer=observer,
        )
        if not ok:
            return False, err, fetched
        ok, err = verify_ref(repo, base_ref)
        if not ok:
            return False, err, fetched
        return True, None, fetched

    ok, err = verify_ref(repo, base_ref)
    if not ok:
        return (
            False,
            err
            or HookError("ensure_base_ref", f"Cannot resolve base ref '{base_ref}'"),
            False,
        )
    return True, None, False


def merge_base(
    repo: Path, base_ref: str, *, commit: str = "HEAD"
) -> tuple[str | None, HookError | None]:
    """Compute the merge-base of base_ref and a commit.

    Parameters
    ----------
    repo
        Repository root path.
    base_ref
        Base ref to compare against.
    commit
        Commit the base ref is compared with. The default follows the live
        ``HEAD``; a caller that captured a commit earlier passes it here, so a
        checkout that moves mid-run cannot change what the comparison means.

    Returns
    -------
    tuple[str | None, HookError | None]
        Merge-base commit hash and hook error, if any.

    """
    p = run(["git", "merge-base", base_ref, commit], repo)
    if p.returncode != 0:
        error_output = p.stderr.strip() or p.stdout.strip()
        return (
            None,
            HookError(
                "merge_base",
                f"git merge-base {base_ref} {commit} failed: {error_output}",
            ),
        )
    base = p.stdout.strip()
    if not base:
        return None, HookError("merge_base", "git merge-base returned empty output")
    return base, None


def current_branch(repo: Path) -> tuple[str | None, HookError | None]:
    """Return the current branch name.

    Parameters
    ----------
    repo
        Repository path.

    Returns
    -------
    tuple[str | None, HookError | None]
        Branch name and no error on success, or ``(None, error)`` on failure.

    """
    p = run(["git", "branch", "--show-current"], repo)
    if p.returncode != 0:
        error_output = p.stderr.strip() or p.stdout.strip()
        return None, HookError(
            "current_branch", f"git branch --show-current failed: {error_output}"
        )
    branch = p.stdout.strip()
    if not branch:
        # A detached HEAD exits 0 with no name; that is not a command failure.
        return None, None
    return branch, None


def head_commit(repo: Path) -> tuple[str | None, HookError | None]:
    """Return the current ``HEAD`` commit hash.

    The decision log records the observed commit so a run can be explained
    against the history it actually saw. A read failure is reported rather
    than raised: the fact is log-only and must never change a decision.

    Parameters
    ----------
    repo
        Repository path.

    Returns
    -------
    tuple[str | None, HookError | None]
        Commit hash and no error on success, or ``(None, error)`` on failure.

    """
    p = run(["git", "rev-parse", "--verify", "HEAD^{commit}"], repo)
    if p.returncode != 0:
        error_output = p.stderr.strip() or p.stdout.strip()
        return None, HookError(
            "head_commit", f"git rev-parse HEAD failed: {error_output}"
        )
    commit = p.stdout.strip()
    if not commit:
        return None, HookError(
            "head_commit", "git rev-parse HEAD returned empty output"
        )
    return commit, None


def remote_url(repo: Path, remote: str) -> tuple[str | None, HookError | None]:
    """Return the configured URL for a Git remote.

    Parameters
    ----------
    repo
        Repository path.
    remote
        Remote name.

    Returns
    -------
    tuple[str | None, HookError | None]
        Remote URL and no error on success, or ``(None, error)`` on failure.

    """
    p = run(["git", "remote", "get-url", remote], repo)
    if p.returncode != 0:
        error_output = p.stderr.strip() or p.stdout.strip()
        return None, HookError(
            "remote_url", f"git remote get-url {remote} failed: {error_output}"
        )
    url = p.stdout.strip()
    if not url:
        return None, HookError(
            "remote_url", f"git remote get-url {remote} returned empty output"
        )
    return url, None


def is_ancestor(
    repo: Path, ancestor: str, descendant: str
) -> tuple[bool | None, HookError | None]:
    """Return whether one commit is an ancestor of another.

    Parameters
    ----------
    repo
        Repository path.
    ancestor
        Commit expected to be an ancestor.
    descendant
        Commit expected to descend from ``ancestor``.

    Returns
    -------
    tuple[bool | None, HookError | None]
        ``(True, None)`` when ``ancestor`` is an ancestor, ``(False, None)``
        when it is not, and ``(None, error)`` on Git errors.

    """
    p = run(["git", "merge-base", "--is-ancestor", ancestor, descendant], repo)
    if p.returncode == 0:
        return True, None
    if p.returncode == 1:
        return False, None
    error_output = p.stderr.strip() or p.stdout.strip()
    return None, HookError(
        "is_ancestor", f"git merge-base --is-ancestor failed: {error_output}"
    )


def is_three_way_merge_configured(repo: Path) -> bool:
    """Return whether Git's conflict style is configured for three-way markers."""
    config = run(["git", "config", "--get", "merge.conflictStyle"], repo)
    if config.returncode != 0:
        return False
    return config.stdout.strip() in {"zdiff3", "diff3-zealous"}


def changed_files(
    repo: Path, base_commit: str
) -> tuple[list[str] | None, HookError | None]:
    """List files changed relative to a base commit.

    Parameters
    ----------
    repo
        Repository root path.
    base_commit
        Base commit hash for diffing.

    Returns
    -------
    tuple[list[str] | None, HookError | None]
        Sorted list of changed files and hook error, if any.

    """
    changed: set[str] = set()

    # Tracked changes (unstaged and staged) relative to base_commit
    for args in (
        ["git", "diff", "--name-only", base_commit],
        ["git", "diff", "--cached", "--name-only", base_commit],
    ):
        p = run(args, repo)
        if p.returncode != 0:
            return (
                None,
                HookError(
                    "changed_files",
                    f"{' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}",
                ),
            )
        for line in p.stdout.splitlines():
            line = line.strip()
            if line:
                changed.add(line)

    # Untracked (but not ignored)
    u = run(["git", "ls-files", "--others", "--exclude-standard"], repo)
    if u.returncode != 0:
        return None, HookError(
            "changed_files",
            f"git ls-files failed: {u.stderr.strip() or u.stdout.strip()}",
        )
    for line in u.stdout.splitlines():
        line = line.strip()
        if line:
            changed.add(line)

    return sorted(changed), None


def has_uncommitted_changes(repo: Path) -> tuple[bool | None, HookError | None]:
    """Check whether the working tree has uncommitted or untracked changes.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    tuple[bool | None, HookError | None]
        True if dirty, False if clean, None on error; and an hook error.

    """
    for args in (
        ["git", "diff", "--quiet"],
        ["git", "diff", "--cached", "--quiet"],
    ):
        p = run(args, repo)
        if p.returncode == 1:
            return True, None
        if p.returncode != 0:
            return (
                None,
                HookError(
                    "has_uncommitted_changes",
                    f"{' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}",
                ),
            )

    u = run(["git", "ls-files", "--others", "--exclude-standard"], repo)
    if u.returncode != 0:
        return None, HookError(
            "has_uncommitted_changes",
            f"git ls-files failed: {u.stderr.strip() or u.stdout.strip()}",
        )
    if u.stdout.strip():
        return True, None

    return False, None


def get_upstream_ref(repo: Path) -> tuple[str | None, HookError | None]:
    """Get the upstream tracking ref for the current branch.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    tuple[str | None, HookError | None]
        Upstream ref name (e.g. ``origin/main``) and hook error, if any.

    """
    p = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo)
    if p.returncode != 0:
        return None, HookError(
            "get_upstream_ref",
            p.stderr.strip() or p.stdout.strip() or "no upstream configured",
        )
    ref = p.stdout.strip()
    if not ref:
        return None, HookError(
            "get_upstream_ref", "git rev-parse returned empty upstream"
        )
    return ref, None


def git_config_get(repo: Path, key: str) -> tuple[str | None, HookError | None]:
    """Read one Git configuration value.

    Parameters
    ----------
    repo
        Repository path.
    key
        Configuration key such as ``branch.feature.remote``.

    Returns
    -------
    tuple[str | None, HookError | None]
        Configured value and no error when present. ``(None, None)`` when the
        key is unset, and ``(None, error)`` when the read fails.

    """
    p = run(["git", "config", "--get", key], repo)
    if p.returncode == 1:
        return None, None
    if p.returncode != 0:
        return (
            None,
            HookError(
                "git_config_get",
                p.stderr.strip()
                or p.stdout.strip()
                or f"git config --get {key} failed",
            ),
        )
    value = p.stdout.strip()
    return (value or None), None


def git_config_get_first(repo: Path, key: str) -> tuple[str | None, HookError | None]:
    """Read the first value of a Git configuration key.

    Git's ``@{u}`` uses the first ``branch.<name>.merge`` entry, while
    ``git config --get`` returns the last. Reading every value and taking the
    first keeps the gate comparing the same branch as ``@{u}``.

    Parameters
    ----------
    repo
        Repository path.
    key
        Multi-valued configuration key such as ``branch.feature.merge``.

    Returns
    -------
    tuple[str | None, HookError | None]
        First configured value and no error when present. ``(None, None)``
        when the key is unset, and ``(None, error)`` when the read fails.

    """
    p = run(["git", "config", "--get-all", key], repo)
    if p.returncode == 1:
        return None, None
    if p.returncode != 0:
        return (
            None,
            HookError(
                "git_config_get_first",
                p.stderr.strip()
                or p.stdout.strip()
                or f"git config --get-all {key} failed",
            ),
        )
    values = [line.strip() for line in p.stdout.splitlines() if line.strip()]
    return (values[0] if values else None), None


def resolve_upstream(repo: Path) -> tuple[str | None, str | None, HookError | None]:
    """Resolve the current branch's configured upstream.

    Unlike ``get_upstream_ref``, this reads ``branch.<name>.remote`` directly.
    Git cannot store a URL-valued remote as a remote-tracking branch, so
    ``@{u}`` fails for those upstreams even though they are configured.

    Parameters
    ----------
    repo
        Repository path.

    Returns
    -------
    tuple[str | None, str | None, HookError | None]
        ``(remote, merge_ref, error)``. ``remote`` is a configured remote
        name, a URL, or ``"."`` for a local upstream branch. ``merge_ref`` is
        the fully qualified ref from ``branch.<name>.merge`` (normally
        ``refs/heads/<branch>``), kept qualified so a fetch cannot resolve a
        same-named tag instead of the branch. Returns ``(None, None, None)``
        when the current branch has no upstream.

    """
    branch, branch_err = current_branch(repo)
    if branch_err is not None:
        # A failed branch lookup is not the same as a detached HEAD: the gate
        # must fail open with a warning instead of silently skipping.
        return None, None, branch_err
    if branch is None:
        # A detached HEAD has no configured upstream.
        return None, None, None
    remote, err = git_config_get(repo, f"branch.{branch}.remote")
    if err is not None:
        return None, None, err
    merge_ref, err = git_config_get_first(repo, f"branch.{branch}.merge")
    if err is not None:
        return None, None, err
    if remote is None or merge_ref is None:
        return None, None, None
    # A "." remote names a branch in this repository. There is no remote head
    # to fetch, but the local branch remains an authoritative comparison. The
    # configured ref stays fully qualified: a short branch name is ambiguous
    # when the remote also has a tag with that name.
    return remote, merge_ref, None


#: Prefix for the private refs used to hold a fetched upstream head. A unique
#: ref per fetch isolates the result from any concurrent fetch, which would
#: otherwise overwrite the shared ``FETCH_HEAD``.
def has_unpushed_commits(
    repo: Path, upstream: str
) -> tuple[bool | None, HookError | None]:
    """Check whether ``HEAD`` is ahead of the given upstream ref.

    Parameters
    ----------
    repo
        Repository root path.
    upstream
        Upstream tracking ref to compare against.

    Returns
    -------
    tuple[bool | None, HookError | None]
        True if local commits are ahead, False if not, None on error; and an
        hook error.

    """
    p = run(["git", "rev-list", "--count", f"{upstream}..HEAD"], repo)
    if p.returncode != 0:
        return None, HookError(
            "has_unpushed_commits",
            f"git rev-list --count {upstream}..HEAD failed: "
            f"{p.stderr.strip() or p.stdout.strip()}",
        )

    ahead = p.stdout.strip()
    if not ahead:
        return None, HookError(
            "has_unpushed_commits", "git rev-list --count returned empty output"
        )

    try:
        return int(ahead) > 0, None
    except ValueError:
        return None, HookError(
            "has_unpushed_commits",
            f"git rev-list --count returned non-integer output: {ahead}",
        )
