"""Git plumbing layer.

Provides subprocess execution and git operations:
repository resolution, fetching, ref manipulation, merge-base,
changed-file enumeration, and working-tree queries.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dataclasses.dataclass(slots=True, frozen=True)
class ExcludedPath:
    """Untracked entries a repository query must ignore.

    Attributes
    ----------
    directory
        Directory holding the entries to ignore.
    pattern
        Glob matched against the names of ``directory``'s direct entries. Only
        those direct entries are ignored; a subdirectory, or a file whose name
        does not match, still counts as a repository change.

    """

    directory: Path
    pattern: str = "*"


class Exclusions(typ.Protocol):
    """Supplier of the run's own entries that repository queries ignore."""

    def __call__(self) -> tuple[ExcludedPath, ...] | None:
        """Return the exclusions that apply now, or ``None`` if unresolved."""
        ...


@dataclasses.dataclass(slots=True, frozen=True)
class QueryResult[ValueT]:
    """Outcome of a repository query that can warn without failing.

    Attributes
    ----------
    value
        The query's answer, or ``None`` when Git could not answer.
    error
        The Git failure that prevented an answer, if any.
    warning
        A fixed notice that an exclusion needed its lexical fallback. It is
        separate from ``error`` because the query still answered correctly;
        only the way it resolved the exclusion was degraded.

    """

    value: ValueT | None
    error: str | None = None
    warning: str | None = None


_EXCLUSION_WARNING = "could not resolve the decision-log exclusion"


class RepositoryView:
    """Repository queries that ignore the invoking run's own log entries.

    The decision log writes daily files beneath the XDG state directory. When
    that directory lies inside a worktree, the run's own files would otherwise
    count as untracked changes and change the stop decision. This adapter owns
    that knowledge so callers see repository facts, not exclusion rules.

    Parameters
    ----------
    exclusions
        Supplier asked afresh at every query. Asking late keeps the answer
        current when a run crosses midnight and writes a new daily file after
        an earlier query. Defaults to no exclusions.

    """

    def __init__(self, exclusions: Exclusions | None = None) -> None:
        """Store the exclusion supplier."""
        self._exclusions = exclusions or (lambda: ())

    def _resolve_exclusions(self) -> tuple[tuple[ExcludedPath, ...], str | None]:
        """Ask the supplier, reporting an unresolved answer as a warning.

        The supplier belongs to the decision log, which must never change the
        decision. A supplier that fails, or cannot say which entries it wrote,
        therefore degrades to no exclusions with a fixed warning rather than
        raising into a gate.
        """
        try:
            supplied = self._exclusions()
        except Exception:  # ruff:ignore[blind-except]  # a failed exclusion must not fail a gate.
            return (), _EXCLUSION_WARNING
        if supplied is None:
            return (), _EXCLUSION_WARNING
        return tuple(supplied), None

    def changed_files(self, repo: Path, base_commit: str) -> QueryResult[list[str]]:
        """List changed files, including the run's own log exclusion.

        A fallback answer still carries the exclusion, so the result matches a
        logging-disabled run. The warning says the exclusion was resolved by
        fallback; a missing value is a Git failure and keeps its own error.
        """
        excluded, supplier_warning = self._resolve_exclusions()
        result = changed_files(repo, base_commit, exclude=excluded)
        return dataclasses.replace(result, warning=result.warning or supplier_warning)

    def has_uncommitted_changes(self, repo: Path) -> QueryResult[bool]:
        """Report whether the worktree is dirty, honouring the exclusion."""
        excluded, supplier_warning = self._resolve_exclusions()
        result = has_uncommitted_changes(repo, exclude=excluded)
        return dataclasses.replace(result, warning=result.warning or supplier_warning)


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

    Output is captured as bytes and decoded with :func:`os.fsdecode` rather
    than through text mode. Text mode applies universal-newline translation,
    which would turn a carriage return inside a Git path into a newline;
    ``-z`` path queries build their exclusions from the filesystem, so the two
    would then no longer match. Decoding here also mirrors the filesystem
    encoding Git used for the path bytes.

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
        completed = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # valid: command and args are controlled (no shell, no user-supplied command strings).
            cmd,
            cwd=str(cwd),
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
    return subprocess.CompletedProcess(
        args=completed.args,
        returncode=completed.returncode,
        stdout=os.fsdecode(completed.stdout),
        stderr=os.fsdecode(completed.stderr),
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


def _nearest_existing(path: Path) -> Path:
    """Return ``path`` or its nearest existing ancestor.

    Repository discovery needs a directory that exists. A tracked file whose
    parent directory was deleted still has an index entry, and its nearest
    existing ancestor is the first directory Git can be asked about.
    """
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def _has_git_ancestor(start: Path) -> bool | None:
    """Return whether any directory at or above ``start`` holds ``.git``.

    Checking for the marker before running Git separates "this path is outside
    every worktree", a definite answer, from "Git could not answer". The
    marker is a directory in a normal clone and a file in a linked worktree,
    so existence is the test.

    Both the lexical path and its resolved target are walked. A directory
    reached through a symbolic link that leaves the worktree and re-enters it
    has no ``.git`` among its lexical ancestors, yet Git, which resolves the
    working directory physically, answers from inside the worktree. Walking
    only the lexical path would wrongly report the path as untracked there.

    Returns
    -------
    bool | None
        ``None`` when a symbolic link on the path could not be resolved. The
        resolved ancestors - the ones Git itself uses - were then never
        inspected, so the path's location is unknown rather than outside every
        worktree. Callers must preserve files on that answer.

    """
    candidates = {start}
    try:
        candidates.add(start.resolve())
    except OSError:
        return None
    return any(
        (directory / ".git").exists()
        for candidate in candidates
        for directory in (candidate, *candidate.parents)
    )


def _locate(path: Path) -> tuple[Path | None, bool | None]:
    """Return the worktree root around ``path`` and the marker answer.

    Returns
    -------
    tuple[Path | None, bool | None]
        The root, and ``True`` when one was found. When the second item is
        ``False`` the path is outside every worktree, and ``None`` means the
        location itself is unknown. A ``True`` with ``None`` root means the
        marker exists but Git could not report the root, so callers must treat
        the root as unanswerable rather than absent.

    """
    start = _nearest_existing(path.parent)
    has_ancestor = _has_git_ancestor(start)
    if has_ancestor is not True:
        return None, has_ancestor
    root, _err = repo_root(start)
    return root, True


def is_tracked(path: Path) -> bool | None:
    """Return whether Git tracks ``path``, or ``None`` when that is unknown.

    The decision log uses this when appending and when retention prunes, so it
    never turns a committed daily file into an uncommitted change and never
    deletes committed data. The file itself need not exist: Git tracks a file
    through its index, so a deleted entry is still tracked, and discovery
    starts from the nearest existing ancestor for that case.

    Parameters
    ----------
    path
        File to test.

    Returns
    -------
    bool | None
        ``True`` when Git resolves the path as a tracked file, ``False`` when
        the path is outside every worktree or Git confirms it is untracked, and
        ``None`` when Git could not answer, including an index error or a
        symbolic link that could not be resolved. ``None``
        is not a failure of the hook: callers treat it as "preserve", because
        modifying a file whose ownership is unknown could change the decision.
        An ambiguous path inside a worktree reports ``True`` for the same
        reason.

    """
    root, located = _locate(path)
    if located is not True:
        # No worktree contains the path, so it cannot be tracked. This is the
        # normal case: the XDG state directory lives outside the repository.
        # An unknown location is not that case and must stay unknown.
        return located
    if root is None:
        return None
    relative = _worktree_relative(path, root)
    if relative is None:
        # Git found a worktree, but neither the lexical nor the resolved path
        # lies inside it. Refusing is the only answer that cannot change the
        # hook's decision.
        return True
    listed = run(
        ["git", "ls-files", "--error-unmatch", "--", relative.as_posix()],
        root,
    )
    if listed.returncode == 0:
        return True
    if listed.returncode == 1:
        # ``--error-unmatch`` exits 1 only when the path is absent from the
        # index. Every other non-zero status is a Git failure, not an answer.
        return False
    return None


def is_in_worktree(path: Path) -> bool | None:
    """Return whether a Git worktree contains ``path``, or ``None`` if unknown.

    This answers the location question alone, without consulting the index.
    Retention uses it to keep an expired daily log that lies inside a worktree:
    removing it would change what the uncommitted-changes gate reports, while a
    logging-disabled run would leave it visible. A path outside every worktree
    is ``False``; an unanswerable question is ``None``, which callers treat as
    "preserve".

    Parameters
    ----------
    path
        File to locate. It need not exist.

    Returns
    -------
    bool | None
        ``True`` when Git resolves a worktree around the path, ``False`` when
        it is outside every worktree, and ``None`` when Git could not answer
        or a symbolic link on the path could not be resolved.

    """
    root, located = _locate(path)
    if located is not True:
        return located
    if root is None:
        return None
    # A symlink that escapes the worktree still answers "preserve", which is
    # the safe direction: retention must never delete on a doubtful answer.
    return True


def is_ignored(path: Path) -> bool | None:
    """Return whether Git ignores ``path``, or ``None`` when that is unknown.

    Retention asks this only for a daily log inside a worktree. Ignored paths
    never appear in ``git status`` output, so deleting one cannot change the
    changed-file list or the uncommitted-changes gate; every other file inside
    a worktree is preserved for parity with a logging-disabled run.

    Parameters
    ----------
    path
        File to test. It need not exist: Git matches the name against its
        ignore rules rather than the file's contents.

    Returns
    -------
    bool | None
        ``True`` when an ignore rule matches, ``False`` when Git reports no
        match or the path is outside every worktree, and ``None`` when Git
        could not answer. Only ``True`` lets retention delete, so every other
        answer preserves the file.

    """
    root, located = _locate(path)
    if located is not True:
        return located
    if root is None:
        return None
    relative = _worktree_relative(path, root)
    if relative is None:
        # No route into the worktree exists, so no ignore rule can match the
        # path either. ``False`` preserves the file, which is the safe answer.
        return False
    checked = run(
        ["git", "check-ignore", "--quiet", "--", relative.as_posix()],
        root,
    )
    if checked.returncode == 0:
        return True
    if checked.returncode == 1:
        # ``--quiet`` exits 1 only when no ignore rule matches. Every other
        # non-zero status is a Git failure, not an answer.
        return False
    return None


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


class FetchObserver(typ.Protocol):
    """Observe the boundaries of an actual ``git fetch`` invocation.

    The callbacks run only when a fetch really starts, so a decision log can
    record the command without claiming one that never happened. The token
    returned by ``fetch_started`` is the caller's own progress handle; it is
    handed back with the fetch's real exit code.
    """

    def fetch_started(self) -> object:
        """Record that a fetch is about to run and return a progress token."""
        ...

    def fetch_finished(self, token: object, exit_code: int) -> None:
        """Record the fetch's exit code against the token it returned."""
        ...


def fetch_remote_branch(
    repo: Path,
    remote: str,
    branch: str,
    *,
    observer: FetchObserver | None = None,
) -> tuple[bool, str | None, int]:
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
        Optional observer notified immediately before and after the fetch.

    Returns
    -------
    tuple[bool, str | None, int]
        ``ok`` is ``True`` on success. ``error_msg`` contains the Git error
        string on failure. The final item is the fetch process's exit code.

    """
    token = observer.fetch_started() if observer is not None else None
    fetch = run(["git", "fetch", "--quiet", remote, branch], repo)
    if observer is not None:
        observer.fetch_finished(token, fetch.returncode)
    if fetch.returncode != 0:
        error_output = fetch.stderr.strip() or fetch.stdout.strip()
        error = f"git fetch {remote} {branch} failed: {error_output}"
        return False, error, fetch.returncode
    return True, None, fetch.returncode


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


def ensure_remote_branch_ref(  # ruff:ignore[too-many-arguments]  # repo, remote, branch, policy and observer are all distinct inputs.
    repo: Path,
    remote: str,
    branch: str,
    *,
    always_fetch: bool,
    observer: FetchObserver | None = None,
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
    observer
        Optional observer notified only when a fetch actually runs.

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

    ok, err, _exit_code = fetch_remote_branch(repo, remote, branch, observer=observer)
    if not ok:
        return False, err, fetched
    fetched = True

    exists, err = ref_exists(repo, ref)
    if err:
        return False, err, fetched
    if not exists:
        return False, f"{remote}/{branch} still missing after fetch", fetched
    return True, None, fetched


def ensure_base_ref(  # ruff:ignore[too-many-arguments]  # repo, ref, fetch policy, remote and observer are all distinct inputs.
    repo: Path,
    base_ref: str,
    *,
    always_fetch: bool,
    primary_remote: str = "origin",
    observer: FetchObserver | None = None,
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
    observer
        Optional observer notified only when a fetch actually runs.

    Returns
    -------
    tuple[bool, str | None, bool]
        ok, error message (if any), fetched.

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


def _nul_paths(output: str) -> list[str]:
    r"""Split NUL-separated Git output into non-empty paths.

    ``-z`` makes Git emit raw, unquoted path bytes terminated by NUL. A path
    containing a double quote, a backslash, a tab, a newline, or a carriage
    return therefore still matches the filesystem-derived exclusion;
    ``core.quotePath`` only affects bytes above 0x80. Entries are not stripped
    because leading and trailing spaces are part of the name.
    """
    return [path for path in output.split("\0") if path]


def _lexical_relative(candidate: Path, repo: Path) -> Path | None:
    """Return ``candidate`` relative to ``repo`` without touching the filesystem."""
    try:
        relative = Path(os.path.relpath(candidate, repo))
    except ValueError:
        return None
    if relative.parts and relative.parts[0] == "..":
        return None
    return relative


def _worktree_relative(path: Path, root: Path) -> Path | None:
    """Return ``path`` relative to ``root``, following symbolic links.

    A path reached through an outside symbolic link has no lexical route into
    the worktree, yet Git, which resolves its working directory physically,
    still answers about it. Resolving both sides recovers the relative path
    Git uses, so a tracked file found that way is not mistaken for untracked.
    """
    relative = _lexical_relative(path, root)
    if relative is not None:
        return relative
    try:
        return path.resolve().relative_to(root.resolve())
    except OSError, ValueError:
        return None


def _excluded_rules(
    repo: Path, excluded: cabc.Iterable[ExcludedPath]
) -> tuple[tuple[tuple[str, str], ...], str | None]:
    """Resolve exclusions to repository-relative directories and name globs.

    A directory that cannot be resolved falls back to a lexical relative path
    so the exclusion is preserved. Dropping it would let the hook's own log
    files count as untracked changes and change the decision, which the log
    must never do.

    Returns
    -------
    tuple[tuple[tuple[str, str], ...], str | None]
        Pairs of directory prefix and name glob, and, when resolution needed
        the lexical fallback, a fixed warning. The warning names no path
        because the underlying error can.

    """
    candidates = list(excluded)
    if not candidates:
        return (), None
    warning: str | None = None
    try:
        repo_resolved = repo.resolve()
    except OSError:
        repo_resolved = repo.absolute()
        warning = _EXCLUSION_WARNING
    rules: list[tuple[str, str]] = []
    for candidate in candidates:
        relative, unresolved = _candidate_relative(candidate, repo_resolved)
        if unresolved:
            warning = _EXCLUSION_WARNING
        if relative is None:
            continue
        # A log directory that resolves to the worktree root has the relative
        # path ``.``. Git reports its direct entries with an empty directory
        # component, so normalise the root to ``""`` instead of dropping the
        # rule and leaving the log visible to both queries.
        prefix = relative.as_posix()
        rules.append(("" if prefix == "." else prefix, candidate.pattern))
    return tuple(rules), warning


def _candidate_relative(
    candidate: ExcludedPath, repo_resolved: Path
) -> tuple[Path | None, bool]:
    """Resolve one exclusion to a worktree-relative directory.

    Returns the relative directory, or ``None`` when it cannot affect the
    worktree's status, together with whether the answer depended on a
    resolution failure. Such a directory may be an outside alias pointing into
    the worktree, so the caller must report it rather than drop it silently.
    """
    try:
        resolved_directory = candidate.directory.resolve()
    except OSError:
        # Resolution failed, so the lexical path cannot tell whether this is
        # an alias into the worktree. Apply the lexical rule when there is one
        # and report either outcome.
        return _lexical_relative(candidate.directory, repo_resolved), True
    try:
        return resolved_directory.relative_to(repo_resolved), False
    except ValueError:
        # The directory resolves outside the worktree, so a resolved rule
        # could never match. A lexical route into the worktree is still
        # applied, because Git reports the lexical path for the symlink
        # itself, and that route needs the fixed warning.
        relative = _lexical_relative(candidate.directory, repo_resolved)
        return relative, relative is not None


def _is_excluded_path(path: str, rules: cabc.Iterable[tuple[str, str]]) -> bool:
    """Return whether a path is a direct entry matched by an exclusion rule."""
    directory, _, name = path.rpartition("/")
    return any(
        directory == prefix and fnmatch.fnmatchcase(name, pattern)
        for prefix, pattern in rules
    )


def _tracked_changes(
    repo: Path, base_commit: str
) -> tuple[set[str] | None, str | None]:
    """Return tracked changes (unstaged and staged) relative to a base commit."""
    changed: set[str] = set()
    for args in (
        ["git", "diff", "--name-only", "-z", base_commit],
        ["git", "diff", "--cached", "--name-only", "-z", base_commit],
    ):
        p = run(args, repo)
        if p.returncode != 0:
            return (
                None,
                f"{' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}",
            )
        changed.update(_nul_paths(p.stdout))
    return changed, None


def changed_files(
    repo: Path,
    base_commit: str,
    *,
    exclude: cabc.Iterable[ExcludedPath] = (),
) -> QueryResult[list[str]]:
    """List files changed relative to a base commit.

    Parameters
    ----------
    repo
        Repository root path.
    base_commit
        Base commit hash for diffing.
    exclude
        Untracked entries to ignore. The decision log uses this to hide the
        daily files it writes when its state directory sits inside the
        worktree.

    Returns
    -------
    QueryResult[list[str]]
        Sorted list of changed files and, when the exclusion needed its
        lexical fallback, a warning separate from any Git error. A Git failure
        carries ``None`` and its error instead.

    """
    excluded, exclude_warning = _excluded_rules(repo, exclude)

    changed, tracked_err = _tracked_changes(repo, base_commit)
    if changed is None:
        return QueryResult(None, tracked_err)

    # Untracked (but not ignored)
    u = run(["git", "ls-files", "--others", "--exclude-standard", "-z"], repo)
    if u.returncode != 0:
        return QueryResult(
            None, f"git ls-files failed: {u.stderr.strip() or u.stdout.strip()}"
        )
    changed.update(
        path for path in _nul_paths(u.stdout) if not _is_excluded_path(path, excluded)
    )

    return QueryResult(sorted(changed), warning=exclude_warning)


def has_uncommitted_changes(
    repo: Path, *, exclude: cabc.Iterable[ExcludedPath] = ()
) -> QueryResult[bool]:
    """Check whether the working tree has uncommitted or untracked changes.

    Parameters
    ----------
    repo
        Repository root path.
    exclude
        Untracked entries to ignore. Passing the decision-log daily files
        keeps the hook's own log out of this gate when the configured state
        home lies inside the worktree.

    Returns
    -------
    QueryResult[bool]
        True if dirty, False if clean, and, when the exclusion needed its
        lexical fallback, a warning separate from any Git error. A Git failure
        carries ``None`` and its error instead.

    """
    excluded, exclude_warning = _excluded_rules(repo, exclude)
    for args in (
        ["git", "diff", "--quiet"],
        ["git", "diff", "--cached", "--quiet"],
    ):
        p = run(args, repo)
        if p.returncode == 1:
            return QueryResult(value=True, warning=exclude_warning)
        if p.returncode != 0:
            return QueryResult(
                None,
                f"{' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}",
            )

    u = run(["git", "ls-files", "--others", "--exclude-standard", "-z"], repo)
    if u.returncode != 0:
        return QueryResult(
            None, f"git ls-files failed: {u.stderr.strip() or u.stdout.strip()}"
        )
    dirty = any(not _is_excluded_path(path, excluded) for path in _nul_paths(u.stdout))
    return QueryResult(dirty, warning=exclude_warning)


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
