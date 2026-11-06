"""Git plumbing layer.

Provides subprocess execution and git operations:
repository resolution, fetching, ref manipulation, merge-base,
changed-file enumeration, and working-tree queries.
"""

from __future__ import annotations

import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc


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


def head_commit(repo: Path) -> tuple[str | None, str | None]:
    """Return the current ``HEAD`` commit hash.

    Parameters
    ----------
    repo
        Repository path.

    Returns
    -------
    tuple[str | None, str | None]
        Commit hash and no error on success, or ``(None, error)`` on failure.

    """
    p = run(["git", "rev-parse", "HEAD"], repo)
    if p.returncode != 0:
        error_output = p.stderr.strip() or p.stdout.strip()
        return None, f"git rev-parse HEAD failed: {error_output}"
    commit = p.stdout.strip()
    if not commit:
        return None, "git rev-parse HEAD returned empty output"
    return commit, None


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


def _git_lines(output: str) -> list[str]:
    r"""Split Git's newline-delimited output into lines.

    ``str.splitlines`` also splits on Unicode line separators such as NEL,
    which are legal in filenames. Git separates records with ``\n`` only, so
    splitting on that character keeps such a path intact.
    """
    return output.split("\n")


def _clean_lines(output: str) -> list[str]:
    """Return the non-empty, stripped lines of Git's output."""
    return [line.strip() for line in _git_lines(output) if line.strip()]


# Git quotes path bytes outside ASCII by default, so a path such as
# ``caf\303\251`` would never match the exclusion prefix derived from the
# filesystem. Ask for raw paths instead. The option is passed per invocation
# rather than through the environment so the behaviour is visible at each
# call site and cannot leak into unrelated Git commands.
_RAW_PATH_ARGS = ("git", "-c", "core.quotePath=false")


def _excluded_prefixes(
    repo: Path, excluded: cabc.Iterable[Path]
) -> tuple[tuple[str, ...], str | None]:
    """Return repository-relative prefixes for directories to ignore.

    Returns
    -------
    tuple[tuple[str, ...], str | None]
        The prefixes and, when a path could not be resolved, an error message.
        The caller decides how to recover; silently dropping the exclusion
        would hide an environmental failure.

    """
    try:
        repo_resolved = repo.resolve()
    except OSError as exc:
        return (), f"could not resolve the repository path: {exc}"
    prefixes: list[str] = []
    for candidate in excluded:
        try:
            relative = candidate.resolve().relative_to(repo_resolved)
        except OSError as exc:
            return (), f"could not resolve an excluded path: {exc}"
        except ValueError:
            # A directory outside the worktree cannot affect its status.
            continue
        prefix = relative.as_posix()
        if prefix not in {"", "."}:
            prefixes.append(prefix)
    return tuple(prefixes), None


def _is_excluded_path(path: str, prefixes: cabc.Iterable[str]) -> bool:
    """Return whether a repository-relative path lies under an excluded prefix."""
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def _tracked_changes(
    repo: Path, base_commit: str
) -> tuple[set[str] | None, str | None]:
    """Return tracked changes (unstaged and staged) relative to a base commit."""
    changed: set[str] = set()
    for args in (
        [*_RAW_PATH_ARGS, "diff", "--name-only", base_commit],
        [*_RAW_PATH_ARGS, "diff", "--cached", "--name-only", base_commit],
    ):
        p = run(args, repo)
        if p.returncode != 0:
            return (
                None,
                f"{' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}",
            )
        changed.update(_clean_lines(p.stdout))
    return changed, None


def changed_files(
    repo: Path,
    base_commit: str,
    *,
    exclude: cabc.Iterable[Path] = (),
) -> tuple[list[str] | None, str | None]:
    """List files changed relative to a base commit.

    Parameters
    ----------
    repo
        Repository root path.
    base_commit
        Base commit hash for diffing.
    exclude
        Directories whose untracked contents must be ignored. The decision log
        uses this when its state directory happens to sit inside the worktree.

    Returns
    -------
    tuple[list[str] | None, str | None]
        Sorted list of changed files and error message, if any.

    """
    excluded, exclude_err = _excluded_prefixes(repo, exclude)
    if exclude_err is not None:
        return None, exclude_err

    changed, tracked_err = _tracked_changes(repo, base_commit)
    if changed is None:
        return None, tracked_err

    # Untracked (but not ignored)
    u = run([*_RAW_PATH_ARGS, "ls-files", "--others", "--exclude-standard"], repo)
    if u.returncode != 0:
        return None, f"git ls-files failed: {u.stderr.strip() or u.stdout.strip()}"
    changed.update(
        line for line in _clean_lines(u.stdout) if not _is_excluded_path(line, excluded)
    )

    return sorted(changed), None


def has_uncommitted_changes(
    repo: Path, *, exclude: cabc.Iterable[Path] = ()
) -> tuple[bool | None, str | None]:
    """Check whether the working tree has uncommitted or untracked changes.

    Parameters
    ----------
    repo
        Repository root path.
    exclude
        Directories whose untracked contents must be ignored. Passing the
        decision-log directory keeps the hook's own log out of this gate when
        the configured state home lies inside the worktree.

    Returns
    -------
    tuple[bool | None, str | None]
        True if dirty, False if clean, None on error; and an error message.

    """
    excluded, exclude_err = _excluded_prefixes(repo, exclude)
    if exclude_err is not None:
        return None, exclude_err
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

    u = run([*_RAW_PATH_ARGS, "ls-files", "--others", "--exclude-standard"], repo)
    if u.returncode != 0:
        return None, f"git ls-files failed: {u.stderr.strip() or u.stdout.strip()}"
    if any(
        line.strip() and not _is_excluded_path(line.strip(), excluded)
        for line in _git_lines(u.stdout)
    ):
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
