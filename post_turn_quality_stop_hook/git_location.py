"""Answer where a path sits in relation to Git worktrees and ignore rules.

The decision log may only write or delete a file where no hook decision can
observe it. Answering that needs three questions about a path: whether a
worktree contains it, whether Git tracks it, and whether an ignore rule matches
it. Each answer is tri-state. ``True`` and ``False`` are Git's own answers, and
``None`` means Git could not answer, which every caller treats as "preserve"
because acting on an unknown location could change the hook's decision.

These queries are separate from the plumbing in `git.py` because they answer a
filesystem-location question rather than reading branch or commit state.
Callers that write or delete a decision-log file import this module directly;
`git.py` no longer re-exports the queries.
"""

from __future__ import annotations

import os
import typing as typ
from pathlib import Path

from post_turn_quality_stop_hook.git import repo_root
from post_turn_quality_stop_hook.process import LaunchedProcess, run

if typ.TYPE_CHECKING:
    import subprocess


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

    The filesystem and Git both resolve a path physically, so a lexical ``..``
    after a symbolic link names a different file from the one an index or
    ignore query would inspect. Resolving both sides first keeps those queries
    about the file Git sees. ``Path.resolve`` also expands the existing prefix
    of a path that does not exist yet, which is what the queries need.

    Returns
    -------
    Path | None
        The relative path. A resolved path outside the worktree keeps its
        lexical form, the only name Git could match for a file that lives
        elsewhere. ``None`` means no route into the worktree exists, or a
        symbolic link on the path could not be resolved.

    """
    try:
        resolved = path.resolve()
        resolved_root = root.resolve()
    except OSError:
        return None
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError:
        return _lexical_relative(path, root)
    return relative


def _entry_relative(path: Path, root: Path) -> Path | None:
    """Return ``path`` relative to ``root``, resolving only its parent.

    Resolving the parent keeps the answer about the physical directory Git
    indexes, while leaving the final component symbolic lets Git answer for the
    directory entry itself. A symbolic link's target is a different file from
    the link, and only the link is the index entry.

    Returns
    -------
    Path | None
        The relative entry name, or ``None`` when no route into the worktree
        exists or a parent symbolic link could not be resolved.

    """
    try:
        parent = path.parent.resolve()
        resolved_root = root.resolve()
    except OSError:
        return None
    try:
        return (parent / path.name).relative_to(resolved_root)
    except ValueError:
        return _lexical_relative(path, root)


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


#: Environment variables that name a repository Git would otherwise discover
#: only through a ``.git`` marker.
_EXPLICIT_REPOSITORY_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE")


def _names_explicit_repository() -> bool:
    """Return whether the environment names a repository without a marker.

    A worktree configured only through ``GIT_DIR`` and ``GIT_WORK_TREE``
    carries no ``.git`` marker, so the marker walk cannot tell "outside every
    worktree" from "configured by the environment". Only Git's own answer
    settles that configuration.
    """
    return any(os.environ.get(name) for name in _EXPLICIT_REPOSITORY_VARIABLES)


def _locate(path: Path) -> tuple[Path | None, bool | None]:
    """Return the worktree root around ``path`` and the marker answer.

    A worktree named only through ``GIT_DIR`` and ``GIT_WORK_TREE`` holds no
    ``.git`` marker for the ancestor walk to find. In that configuration a
    missing marker is inconclusive, so Git is asked directly; a query that
    still cannot answer leaves the location unknown rather than outside every
    worktree, because callers must preserve files on an unknown location.

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
    if has_ancestor is True:
        root, _err = repo_root(start)
        return root, True
    if has_ancestor is False and _names_explicit_repository():
        # Git reads these variables itself, so only its answer is authoritative.
        root, _err = repo_root(start)
        if root is None:
            return None, None
        return root, True
    return None, has_ancestor


def is_tracked(path: Path) -> bool | None:  # ruff:ignore[too-many-return-statements]  # each unknown answer has its own cause.
    """Return whether Git tracks ``path``, or ``None`` when that is unknown.

    The decision log uses this when appending and when retention prunes, so it
    never turns a committed daily file into an uncommitted change and never
    deletes committed data. The file itself need not exist: Git tracks a file
    through its index, so a deleted entry is still tracked, and discovery
    starts from the nearest existing ancestor for that case.

    The query resolves the whole path, so it answers for the file a symbolic
    link points at. A link outside every worktree can still point at a
    committed file inside one, so the target's own worktree is discovered
    before the outside answer is returned; otherwise the file's tracked status
    would be reported as ``False`` and an append could modify committed data.

    Parameters
    ----------
    path
        File to test.

    Returns
    -------
    bool | None
        ``True`` when Git resolves the path as a tracked file, ``False`` when
        the path is outside every worktree or Git confirms it is untracked, and
        ``None`` when Git could not answer, including an index error, a failed
        Git launch, or a symbolic link that could not be resolved. ``None``
        is not a failure of the hook: callers treat it as "preserve", because
        modifying a file whose ownership is unknown could change the decision.
        An ambiguous path inside a worktree reports ``True`` for the same
        reason.

    """
    root, located = _locate(path)
    if located is True:
        if root is None:
            return None
        return _tracked_relative(path, root)
    if located is None:
        return None
    # No worktree contains the path lexically. It may still be a link into one,
    # so the target's own location decides. A link that cannot be resolved
    # leaves the answer unknown rather than reading as outside every worktree.
    target = _resolved_target(path)
    if target is None:
        return None
    target_root, target_located = _locate(target)
    if target_located is not True:
        return target_located
    if target_root is None:
        return None
    return _tracked_relative(target, target_root)


def _tracked_relative(path: Path, root: Path) -> bool | None:
    """Answer whether ``path`` is tracked in the worktree at ``root``."""
    relative = _worktree_relative(path, root)
    if relative is None:
        # Git found a worktree, but neither the lexical nor the resolved path
        # lies inside it. Refusing is the only answer that cannot change the
        # hook's decision.
        return True
    # ``ls-files`` parses its pathname arguments as pathspecs, so a directory
    # literally named ``state*`` would glob-match unrelated entries. The
    # ``literal`` magic makes the query name exactly one file.
    literal_pathspec = f":(literal){relative.as_posix()}"
    listed = run(
        ["git", "ls-files", "--error-unmatch", "--", literal_pathspec],
        root,
    )
    return _index_answer(listed)


def _resolved_target(path: Path) -> Path | None:
    """Return ``path`` with every symbolic link resolved, or ``None``.

    ``None`` means a link on the path could not be resolved, so no location
    claim can be made about the file.
    """
    try:
        return path.resolve()
    except OSError:
        return None


def _index_answer(listed: subprocess.CompletedProcess[str]) -> bool | None:
    """Turn one ``ls-files --error-unmatch`` result into a tri-state answer.

    ``--error-unmatch`` exits 1 when the path is absent from the index, but it
    also exits 1 when the process never ran: ``process.run`` reports a
    synthetically failed launch with status 1 and a diagnostic instead of
    raising. Treating that as an index miss would report a file Git never saw
    as untracked, so a launch failure is reported as an unknown answer, which
    every caller preserves.
    """
    if listed.returncode == 0:
        return True
    if listed.returncode != 1:
        return None
    if isinstance(listed, LaunchedProcess) and not listed.launched:
        return None
    return False


def is_tracked_entry(path: Path) -> bool | None:
    """Return whether Git tracks the directory entry ``path`` names.

    Retention removes a directory entry, so it must ask about that entry rather
    than about whatever a symbolic link at the name resolves to. A tracked
    symlink whose target is untracked and ignored would otherwise pass both
    queries that authorise a deletion, and removing it would delete a committed
    file from the worktree — a change the uncommitted-changes gate reports.

    The final component is therefore left unresolved: an index records names,
    and Git matches the name it is given. Only the parent is resolved, which is
    what makes the name describe the physical directory Git indexes.

    Parameters
    ----------
    path
        Directory entry to test. It need not exist.

    Returns
    -------
    bool | None
        ``True`` when the entry is in the index, ``False`` when the path is
        outside every worktree or Git confirms the entry is absent from it, and
        ``None`` when Git could not answer. Callers must preserve the entry on
        anything but a confirmed ``False``.

    """
    root, located = _locate(path)
    if located is not True:
        return located
    if root is None:
        return None
    relative = _entry_relative(path, root)
    if relative is None:
        return True
    literal_pathspec = f":(literal){relative.as_posix()}"
    listed = run(
        ["git", "ls-files", "--error-unmatch", "--", literal_pathspec],
        root,
    )
    return _index_answer(listed)


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
    """Return whether Git ignores the directory entry ``path`` names.

    Retention asks this only for a daily log inside a worktree. Ignored paths
    never appear in ``git status`` output, so deleting one cannot change the
    changed-file list or the uncommitted-changes gate; every other file inside
    a worktree is preserved for parity with a logging-disabled run.

    The ignore question is resolved on the entry, not on a symbolic link's
    target, matching the path semantics :func:`is_tracked_entry` already uses.
    Retention removes the directory entry, so the entry is what must be
    ignored: resolving the whole path would ask Git about the link's target,
    and an untracked link whose target is ignored would then be deleted even
    though Git lists the link itself as an uncommitted change.

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
    relative = _entry_relative(path, root)
    if relative is None:
        # No route into the worktree exists, so no ignore rule can match the
        # path either. ``False`` preserves the file, which is the safe answer.
        return False
    return _ignore_answer(root, relative)


def _ignore_answer(root: Path, relative: Path) -> bool | None:
    """Turn one ``git check-ignore`` result into a tri-state answer.

    ``--quiet`` exits 0 when a rule matches and 1 when none does, but it also
    exits 1 when the process never ran: ``process.run`` reports a failed launch
    with that synthetic status. Reading that as "no rule matches" would report
    a path Git never saw as unignored, so a launch failure is an unknown answer
    that every caller preserves.
    """
    # ``./`` stops a leading ``:`` from being parsed as pathspec magic, so the
    # check tests the literal entry. A state home under a directory named ``:``
    # would otherwise hand Git a ``:/...`` prefix, which it reads as
    # top-relative magic and matches against the wrong path.
    checked = run(
        ["git", "check-ignore", "--quiet", "--", f"./{relative.as_posix()}"],
        root,
    )
    if checked.returncode == 0:
        return True
    if checked.returncode != 1:
        # Every other non-zero status is a Git failure, not an answer.
        return None
    if isinstance(checked, LaunchedProcess) and not checked.launched:
        return None
    # ``--quiet`` exits 1 only when no ignore rule matches.
    return False


def would_appear_as_change(path: Path) -> bool | None:
    """Return whether a worktree could report ``path`` as an untracked change.

    The decision log asks this before it creates a daily file. Logging must
    never change the hook's decision, and the uncommitted-changes gate reports
    untracked entries, so the log may only be written where that gate cannot
    see it: outside every worktree, or at a path Git ignores. Writing a file
    inside a worktree that Git does not ignore would make a logging-enabled
    run block where a logging-disabled run passes, and hiding such a file again
    would let a pre-existing one pass where a logging-disabled run blocks.

    Parameters
    ----------
    path
        File to test. It need not exist: the answer comes from Git's location
        and ignore rules, not from the file's contents.

    Returns
    -------
    bool | None
        ``True`` when a worktree would report the path, ``False`` when no
        worktree contains it or Git ignores it, and ``None`` when Git could
        not answer. Only ``False`` permits a write; every other answer refuses
        it, because a refused log cannot change a decision.

    """
    located = is_in_worktree(path)
    if located is not True:
        # ``False`` means no worktree can report the path. ``None`` is not
        # that case and must stay unknown, so the caller refuses the write.
        return located
    ignored = is_ignored(path)
    if ignored is True:
        return False
    if ignored is False:
        return True
    return None
