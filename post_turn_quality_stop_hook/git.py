"""Git plumbing layer.

Provides subprocess execution and git operations:
repository resolution, fetching, ref manipulation, merge-base,
changed-file enumeration, and working-tree queries.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import uuid
from pathlib import Path

#: User information in a URL-style remote, up to the last unescaped ``@`` in
#: the authority. Git splits userinfo at that ``@``, which lets a raw ``@``
#: survive in a password. Matching cannot cross whitespace or ``/``, so the
#: redaction stays inside the authority and never consumes diagnostic text.
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^\s/]*@")


def redact_credentials(text: str) -> str:
    """Remove URL user information from arbitrary text.

    Git diagnostics can repeat a configured URL in a shortened form that omits
    the repository path, so replacing the configured remote verbatim is not
    enough. Applying the pattern to the whole message removes every
    ``scheme://userinfo@`` occurrence while leaving unrelated text intact.

    Parameters
    ----------
    text
        Text that may contain URL-style remotes.

    Returns
    -------
    str
        The text with all URL user information removed.

    """
    return _URL_USERINFO.sub(r"\g<scheme>", text)


def redact_remote(remote: str) -> str:
    """Remove user information from a remote URL before display or logging.

    Credentials can appear in ``branch.<name>.remote`` when a tokenised URL
    was used with ``git push -u``. The gate must never echo them in a block
    payload or warning. Remote names, local paths, and scp-style remotes such
    as ``git@github.com:owner/repo.git`` carry no URL userinfo and are
    returned unchanged.

    Parameters
    ----------
    remote
        Configured remote name, URL, or path.

    Returns
    -------
    str
        The remote with any ``scheme://userinfo@`` prefix removed.

    """
    return redact_credentials(remote)


def _subprocess_env() -> dict[str, str]:
    """Return an environment with enriched PATH for subprocess execution.

    The hook may run under a restricted PATH (for example, when launched
    as a Claude Code hook).  Prepend standard user-local tool directories
    so that ``make`` and ``netsuke`` can find ``ruff``, ``ty``,
    ``markdownlint-cli2``, and similar quality tools.

    Returns
    -------
    dict[str, str]
        Environment mapping with an extended ``PATH``.

    """
    env = os.environ.copy()
    home = Path.home()
    extra_dirs = [
        home / ".local" / "bin",
        home / ".bun" / "bin",
        home / ".cargo" / "bin",
        home / ".lody" / "bin",
        home / "go" / "bin",
    ]
    existing_path = env.get("PATH", "")
    entries = existing_path.split(os.pathsep)
    for d in extra_dirs:
        d_str = str(d)
        if d.is_dir() and d_str not in entries:
            entries.insert(0, d_str)
    env["PATH"] = os.pathsep.join(entries)
    return env


#: Seconds a timed-out command is given to exit after ``SIGTERM`` before the
#: whole process group is killed. Git writes ref locks while fetching, so an
#: immediate ``SIGKILL`` can leave a lock behind that blocks later fetches.
_TERMINATION_GRACE_SECONDS = 2.0

#: Seconds allowed for deleting the temporary fetch ref. Deleting a ref runs
#: the repository's ``reference-transaction`` hook, so a hung hook must not be
#: able to stall the hook after the fetch itself has been bounded.
_REF_CLEANUP_TIMEOUT_SECONDS = 5.0

#: Process-group signalling is POSIX-only. Windows has neither ``os.getpgid``
#: nor ``os.killpg``, so the direct child is signalled there instead.
_SUPPORTS_PROCESS_GROUPS = hasattr(os, "getpgid") and hasattr(os, "killpg")


def _process_group_id(proc: subprocess.Popen[str]) -> int | None:
    """Return the process group to signal, or ``None`` if it has gone."""
    if not _SUPPORTS_PROCESS_GROUPS:
        return None
    try:
        return os.getpgid(proc.pid)
    except ProcessLookupError, PermissionError:
        return None


def _signal_process(
    proc: subprocess.Popen[str], pgid: int | None, sig: signal.Signals
) -> None:
    """Signal a process group, falling back to the direct child.

    ``subprocess.run`` only signals the direct child. A ``git fetch`` over SSH
    leaves the SSH transport running in the same process group, so the whole
    group is signalled first. The fallback covers a group that vanished before
    it could be signalled, and a child that already exited is ignored.
    """
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError, PermissionError:
            pass
        else:
            return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        proc.send_signal(sig)


def _wait_for_exit(proc: subprocess.Popen[str], timeout: float) -> bool:
    """Wait for a process to exit, returning whether it did so in time."""
    try:
        proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _close_streams(proc: subprocess.Popen[str]) -> None:
    """Close captured pipes once the child has exited.

    A child that survived both termination signals may still have a reader
    thread blocked on its pipe. Closing the buffered stream then waits for that
    thread's lock without a timeout, which would hang the hook instead of
    reporting the timeout. Leaving a live child's streams to process teardown
    is bounded: the hook exits immediately after reporting the timeout.
    """
    if proc.poll() is None:
        return
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    """Stop a timed-out command and any transport it started.

    ``SIGTERM`` comes first so Git can remove lock files and close transports
    cleanly. Only a command that ignores the grace period is killed outright.
    Both waits are bounded, so a wedged process cannot stall the hook.
    """
    pgid = _process_group_id(proc)
    _signal_process(proc, pgid, signal.SIGTERM)
    if _wait_for_exit(proc, _TERMINATION_GRACE_SECONDS):
        return
    sigkill = getattr(signal, "SIGKILL", None)
    if sigkill is None:
        # Windows defines no SIGKILL; ``kill`` is the strongest stop available.
        proc.kill()
    else:
        _signal_process(proc, pgid, sigkill)
    if not _wait_for_exit(proc, _TERMINATION_GRACE_SECONDS):
        _close_streams(proc)


def _run_with_timeout(
    cmd: list[str], cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a command in its own process group so a timeout kills transports."""
    proc = subprocess.Popen(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: command and args are controlled (no shell, no user-supplied command strings).
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_subprocess_env(),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,
            stdout="",
            stderr=f"command timed out after {timeout} seconds",
        )
    return subprocess.CompletedProcess(
        args=cmd, returncode=proc.returncode, stdout=stdout, stderr=stderr
    )


def run(
    cmd: list[str], cwd: Path, *, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command in the given working directory.

    Parameters
    ----------
    cmd
        Command and arguments to run.
    cwd
        Working directory for the subprocess.
    timeout
        Optional timeout in seconds. A timed-out command is reported as a
        failed process so callers can apply their normal error handling.

    Returns
    -------
    subprocess.CompletedProcess[str]
        Completed process with captured output.

    """
    try:
        if timeout is not None:
            return _run_with_timeout(cmd, cwd, timeout)
        return subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: command and args are controlled (no shell, no user-supplied command strings).
            cmd,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            env=_subprocess_env(),
        )
    except FileNotFoundError as exc:
        if Path(exc.filename or "") != cwd:
            raise
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr=str(exc)
        )
    except NotADirectoryError as exc:
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr=str(exc)
        )


def repo_root(start_cwd: Path) -> tuple[Path | None, str | None]:
    """Resolve the git repository root for a starting directory.

    Parameters
    ----------
    start_cwd
        Directory to resolve from.

    Returns
    -------
    tuple[Path | None, str | None]
        Repository root path and error message, if any.

    """
    p = run(["git", "rev-parse", "--show-toplevel"], start_cwd)
    if p.returncode != 0:
        err = p.stderr.strip() or p.stdout.strip() or "not a git repository"
        return None, err
    root = p.stdout.strip()
    if not root:
        return None, "git rev-parse --show-toplevel returned empty output"
    return Path(root), None


def remote_names(repo: Path) -> tuple[list[str] | None, str | None]:
    """Return configured Git remote names in deterministic order.

    Parameters
    ----------
    repo
        Repository path.

    Returns
    -------
    tuple[list[str] | None, str | None]
        Sorted remote names and no error on success. On failure, the names are
        ``None`` and the error message contains Git stderr or stdout.

    """
    remotes = run(["git", "remote"], repo)
    if remotes.returncode != 0:
        return (
            None,
            f"git remote failed: {remotes.stderr.strip() or remotes.stdout.strip()}",
        )
    return sorted(remotes.stdout.split()), None


def primary_remote_name(
    repo: Path, configured: str | None
) -> tuple[str | None, str | None]:
    """Resolve the primary remote from config, origin, or first remote.

    Parameters
    ----------
    repo
        Repository path.
    configured
        Configured remote name, or ``None`` for automatic selection.

    Returns
    -------
    tuple[str | None, str | None]
        Selected remote and error message. Uses ``configured`` when present in
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


def fetch_remote_branch(
    repo: Path, remote: str, branch: str
) -> tuple[bool, str | None]:
    """Fetch one branch from one remote.

    Parameters
    ----------
    repo
        Repository path.
    remote
        Remote name to fetch from.
    branch
        Branch name to fetch.

    Returns
    -------
    tuple[bool, str | None]
        ``ok`` is ``True`` on success. ``error_msg`` contains the Git error
        string on failure.

    """
    fetch = run(["git", "fetch", "--quiet", remote, branch], repo)
    if fetch.returncode != 0:
        error_output = fetch.stderr.strip() or fetch.stdout.strip()
        return False, f"git fetch {remote} {branch} failed: {error_output}"
    return True, None


def ref_exists(repo: Path, ref: str) -> tuple[bool, str | None]:
    """Check whether a ref exists.

    Parameters
    ----------
    repo
        Repository root path.
    ref
        Fully qualified ref name.

    Returns
    -------
    tuple[bool, str | None]
        True if the ref exists, otherwise False and an error if the check failed.

    """
    verify = run(["git", "show-ref", "--verify", "--quiet", ref], repo)
    if verify.returncode == 0:
        return True, None
    if verify.returncode == 1:
        return False, None
    return (
        False,
        verify.stderr.strip() or verify.stdout.strip() or "git show-ref failed",
    )


def verify_ref(repo: Path, ref: str) -> tuple[bool, str | None]:
    """Verify that a ref can be resolved.

    Parameters
    ----------
    repo
        Repository root path.
    ref
        Ref name to verify with rev-parse.

    Returns
    -------
    tuple[bool, str | None]
        ok and error message, if any.

    """
    rp = run(["git", "rev-parse", "--verify", "--quiet", ref], repo)
    if rp.returncode != 0:
        return False, f"Cannot resolve {ref}"
    return True, None


def ensure_remote_branch_ref(
    repo: Path, remote: str, branch: str, *, always_fetch: bool
) -> tuple[bool, str | None, bool]:
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

    Returns
    -------
    tuple[bool, str | None, bool]
        ``ok``, optional error message, and whether a fetch was attempted.

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

    ok, err = fetch_remote_branch(repo, remote, branch)
    if not ok:
        return False, err, fetched
    fetched = True

    exists, err = ref_exists(repo, ref)
    if err:
        return False, err, fetched
    if not exists:
        return False, f"{remote}/{branch} still missing after fetch", fetched
    return True, None, fetched


def ensure_base_ref(
    repo: Path,
    base_ref: str,
    *,
    always_fetch: bool,
    primary_remote: str = "origin",
) -> tuple[bool, str | None, bool]:
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

    Returns
    -------
    tuple[bool, str | None, bool]
        ok, error message (if any), fetched.

    """
    remote_prefix = f"{primary_remote}/"
    if base_ref.startswith(remote_prefix):
        branch = base_ref.removeprefix(remote_prefix)
        ok, err, fetched = ensure_remote_branch_ref(
            repo, primary_remote, branch, always_fetch=always_fetch
        )
        if not ok:
            return False, err, fetched
        ok, err = verify_ref(repo, base_ref)
        if not ok:
            return False, err, fetched
        return True, None, fetched

    ok, err = verify_ref(repo, base_ref)
    if not ok:
        return False, err or f"Cannot resolve base ref '{base_ref}'", False
    return True, None, False


def merge_base(repo: Path, base_ref: str) -> tuple[str | None, str | None]:
    """Compute the merge-base of base_ref and HEAD.

    Parameters
    ----------
    repo
        Repository root path.
    base_ref
        Base ref to compare against HEAD.

    Returns
    -------
    tuple[str | None, str | None]
        Merge-base commit hash and error message, if any.

    """
    p = run(["git", "merge-base", base_ref, "HEAD"], repo)
    if p.returncode != 0:
        error_output = p.stderr.strip() or p.stdout.strip()
        return (
            None,
            f"git merge-base {base_ref} HEAD failed: {error_output}",
        )
    base = p.stdout.strip()
    if not base:
        return None, "git merge-base returned empty output"
    return base, None


def current_branch(repo: Path) -> tuple[str | None, str | None]:
    """Return the current branch name.

    Parameters
    ----------
    repo
        Repository path.

    Returns
    -------
    tuple[str | None, str | None]
        Branch name and no error on success, or ``(None, error)`` on failure.

    """
    p = run(["git", "branch", "--show-current"], repo)
    if p.returncode != 0:
        error_output = p.stderr.strip() or p.stdout.strip()
        return None, f"git branch --show-current failed: {error_output}"
    branch = p.stdout.strip()
    if not branch:
        # A detached HEAD exits 0 with no name; that is not a command failure.
        return None, None
    return branch, None


def remote_url(repo: Path, remote: str) -> tuple[str | None, str | None]:
    """Return the configured URL for a Git remote.

    Parameters
    ----------
    repo
        Repository path.
    remote
        Remote name.

    Returns
    -------
    tuple[str | None, str | None]
        Remote URL and no error on success, or ``(None, error)`` on failure.

    """
    p = run(["git", "remote", "get-url", remote], repo)
    if p.returncode != 0:
        error_output = p.stderr.strip() or p.stdout.strip()
        return None, f"git remote get-url {remote} failed: {error_output}"
    url = p.stdout.strip()
    if not url:
        return None, f"git remote get-url {remote} returned empty output"
    return url, None


def is_ancestor(
    repo: Path, ancestor: str, descendant: str
) -> tuple[bool | None, str | None]:
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
    tuple[bool | None, str | None]
        ``(True, None)`` when ``ancestor`` is an ancestor, ``(False, None)``
        when it is not, and ``(None, error)`` on Git errors.

    """
    p = run(["git", "merge-base", "--is-ancestor", ancestor, descendant], repo)
    if p.returncode == 0:
        return True, None
    if p.returncode == 1:
        return False, None
    error_output = p.stderr.strip() or p.stdout.strip()
    return None, f"git merge-base --is-ancestor failed: {error_output}"


def is_three_way_merge_configured(repo: Path) -> bool:
    """Return whether Git's conflict style is configured for three-way markers."""
    config = run(["git", "config", "--get", "merge.conflictStyle"], repo)
    if config.returncode != 0:
        return False
    return config.stdout.strip() in {"zdiff3", "diff3-zealous"}


def changed_files(repo: Path, base_commit: str) -> tuple[list[str] | None, str | None]:
    """List files changed relative to a base commit.

    Parameters
    ----------
    repo
        Repository root path.
    base_commit
        Base commit hash for diffing.

    Returns
    -------
    tuple[list[str] | None, str | None]
        Sorted list of changed files and error message, if any.

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
                f"{' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}",
            )
        for line in p.stdout.splitlines():
            line = line.strip()
            if line:
                changed.add(line)

    # Untracked (but not ignored)
    u = run(["git", "ls-files", "--others", "--exclude-standard"], repo)
    if u.returncode != 0:
        return None, f"git ls-files failed: {u.stderr.strip() or u.stdout.strip()}"
    for line in u.stdout.splitlines():
        line = line.strip()
        if line:
            changed.add(line)

    return sorted(changed), None


def has_uncommitted_changes(repo: Path) -> tuple[bool | None, str | None]:
    """Check whether the working tree has uncommitted or untracked changes.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    tuple[bool | None, str | None]
        True if dirty, False if clean, None on error; and an error message.

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
                f"{' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}",
            )

    u = run(["git", "ls-files", "--others", "--exclude-standard"], repo)
    if u.returncode != 0:
        return None, f"git ls-files failed: {u.stderr.strip() or u.stdout.strip()}"
    if u.stdout.strip():
        return True, None

    return False, None


def get_upstream_ref(repo: Path) -> tuple[str | None, str | None]:
    """Get the upstream tracking ref for the current branch.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    tuple[str | None, str | None]
        Upstream ref name (e.g. ``origin/main``) and error message, if any.

    """
    p = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo)
    if p.returncode != 0:
        return None, p.stderr.strip() or p.stdout.strip() or "no upstream configured"
    ref = p.stdout.strip()
    if not ref:
        return None, "git rev-parse returned empty upstream"
    return ref, None


def git_config_get(repo: Path, key: str) -> tuple[str | None, str | None]:
    """Read one Git configuration value.

    Parameters
    ----------
    repo
        Repository path.
    key
        Configuration key such as ``branch.feature.remote``.

    Returns
    -------
    tuple[str | None, str | None]
        Configured value and no error when present. ``(None, None)`` when the
        key is unset, and ``(None, error)`` when the read fails.

    """
    p = run(["git", "config", "--get", key], repo)
    if p.returncode == 1:
        return None, None
    if p.returncode != 0:
        return (
            None,
            p.stderr.strip() or p.stdout.strip() or f"git config --get {key} failed",
        )
    value = p.stdout.strip()
    return (value or None), None


def git_config_get_first(repo: Path, key: str) -> tuple[str | None, str | None]:
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
    tuple[str | None, str | None]
        First configured value and no error when present. ``(None, None)``
        when the key is unset, and ``(None, error)`` when the read fails.

    """
    p = run(["git", "config", "--get-all", key], repo)
    if p.returncode == 1:
        return None, None
    if p.returncode != 0:
        return (
            None,
            p.stderr.strip()
            or p.stdout.strip()
            or f"git config --get-all {key} failed",
        )
    values = [line.strip() for line in p.stdout.splitlines() if line.strip()]
    return (values[0] if values else None), None


def resolve_upstream(repo: Path) -> tuple[str | None, str | None, str | None]:
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
    tuple[str | None, str | None, str | None]
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
_TEMP_FETCH_REF_PREFIX = "refs/post-turn-quality/fetch"


def _qualify_upstream_ref(upstream_ref: str) -> str:
    """Return a fully qualified ref for a configured upstream.

    ``branch.<name>.merge`` normally stores ``refs/heads/<branch>``. Callers
    that pass the short branch name are qualified the same way, so the fetch
    can never resolve a same-named tag instead.
    """
    if upstream_ref.startswith("refs/"):
        return upstream_ref
    return f"refs/heads/{upstream_ref}"


def _delete_temp_ref(repo: Path, temp_ref: str) -> None:
    """Delete a private fetch ref, ignoring a missing ref or cleanup failure."""
    with contextlib.suppress(OSError):
        run(
            ["git", "update-ref", "-d", temp_ref],
            repo,
            timeout=_REF_CLEANUP_TIMEOUT_SECONDS,
        )


def fetch_upstream_head(
    repo: Path, remote: str, upstream_ref: str, *, timeout: float | None = None
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
    timeout
        Optional subprocess timeout in seconds.

    Returns
    -------
    tuple[str | None, str | None]
        Fetched head commit and no error on success, or ``(None, error)``.

    """
    display_remote = redact_remote(remote)
    source_ref = _qualify_upstream_ref(upstream_ref)
    display_ref = source_ref.removeprefix("refs/heads/")
    temp_ref = f"{_TEMP_FETCH_REF_PREFIX}/{uuid.uuid4().hex}"
    try:
        # ``--`` keeps a remote URL or refspec from being read as an option.
        # The explicit fully qualified source ref avoids resolving a tag that
        # shares the branch name, and ``--no-write-fetch-head`` keeps the
        # shared FETCH_HEAD untouched.
        fetch = run(
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
            timeout=timeout,
        )
        if fetch.returncode != 0:
            # Git may echo the configured URL verbatim or in a shortened form.
            error_output = redact_credentials(
                fetch.stderr.strip() or fetch.stdout.strip()
            )
            return None, (
                f"git fetch {display_remote} {display_ref} failed: {error_output}"
            )

        resolved = run(["git", "rev-parse", "--verify", f"{temp_ref}^{{commit}}"], repo)
        if resolved.returncode != 0:
            error_output = resolved.stderr.strip() or resolved.stdout.strip()
            return None, f"git rev-parse {display_ref} failed: {error_output}"
        head = resolved.stdout.strip()
        if not head:
            return None, f"git rev-parse {display_ref} returned empty output"
        return head, None
    finally:
        _delete_temp_ref(repo, temp_ref)


def has_unpushed_commits(repo: Path, upstream: str) -> tuple[bool | None, str | None]:
    """Check whether ``HEAD`` is ahead of the given upstream ref.

    Parameters
    ----------
    repo
        Repository root path.
    upstream
        Upstream tracking ref to compare against.

    Returns
    -------
    tuple[bool | None, str | None]
        True if local commits are ahead, False if not, None on error; and an
        error message.

    """
    p = run(["git", "rev-list", "--count", f"{upstream}..HEAD"], repo)
    if p.returncode != 0:
        return None, (
            f"git rev-list --count {upstream}..HEAD failed: "
            f"{p.stderr.strip() or p.stdout.strip()}"
        )

    ahead = p.stdout.strip()
    if not ahead:
        return None, "git rev-list --count returned empty output"

    try:
        return int(ahead) > 0, None
    except ValueError:
        return None, f"git rev-list --count returned non-integer output: {ahead}"
