"""Git plumbing layer.

Provides subprocess execution and git operations:
repository resolution, fetching, ref manipulation, merge-base,
changed-file enumeration, and working-tree queries.
"""

from __future__ import annotations

import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path


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


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command in the given working directory.

    Parameters
    ----------
    cmd
        Command and arguments to run.
    cwd
        Working directory for the subprocess.

    Returns
    -------
    subprocess.CompletedProcess[str]
        Completed process with captured output.

    """
    try:
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


def ensure_origin_remote(repo: Path) -> tuple[bool, str | None]:
    """Ensure the origin remote is configured.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    tuple[bool, str | None]
        ok and error message, if any.

    """
    remotes = run(["git", "remote"], repo)
    if remotes.returncode != 0:
        return (
            False,
            f"git remote failed: {remotes.stderr.strip() or remotes.stdout.strip()}",
        )
    if "origin" not in remotes.stdout.split():
        return False, "git remote 'origin' not found"
    return True, None


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


def fetch_origin_main(repo: Path) -> tuple[bool, str | None]:
    """Fetch origin/main.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    tuple[bool, str | None]
        ok and error message, if any.

    """
    fetch = run(["git", "fetch", "--quiet", "origin", "main"], repo)
    if fetch.returncode != 0:
        error_output = fetch.stderr.strip() or fetch.stdout.strip()
        return (
            False,
            f"git fetch origin main failed: {error_output}",
        )
    return True, None


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


def ensure_origin_main(
    repo: Path, *, always_fetch: bool
) -> tuple[bool, str | None, bool]:
    """Ensure origin/main is present and resolvable.

    Parameters
    ----------
    repo
        Repository root path.
    always_fetch
        If True, always fetch origin/main.

    Returns
    -------
    tuple[bool, str | None, bool]
        ok, error message (if any), fetched.

    """
    ok, err = ensure_origin_remote(repo)
    if not ok:
        return False, err, False

    ok, err, fetched = ensure_origin_main_ref(repo, always_fetch=always_fetch)
    if not ok:
        return False, err, fetched

    ok, err = verify_ref(repo, "origin/main")
    if not ok:
        return False, err, fetched
    return True, None, fetched


def ensure_origin_main_ref(
    repo: Path, *, always_fetch: bool
) -> tuple[bool, str | None, bool]:
    """Ensure refs/remotes/origin/main exists, fetching if needed.

    Parameters
    ----------
    repo
        Repository root path.
    always_fetch
        If True, always fetch origin/main.

    Returns
    -------
    tuple[bool, str | None, bool]
        ok, error message (if any), fetched.

    """
    fetched = always_fetch
    if not always_fetch:
        exists, err = ref_exists(repo, "refs/remotes/origin/main")
        if err:
            return False, err, fetched
        if exists:
            return True, None, fetched

    ok, err = fetch_origin_main(repo)
    if not ok:
        return False, err, fetched
    fetched = True

    exists, err = ref_exists(repo, "refs/remotes/origin/main")
    if err:
        return False, err, fetched
    if not exists:
        return False, "origin/main still missing after fetch", fetched

    return True, None, fetched


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
        return None, "git branch --show-current returned empty output"
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
