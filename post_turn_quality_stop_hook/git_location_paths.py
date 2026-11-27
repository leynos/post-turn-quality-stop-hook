"""Decide where a path sits in relation to Git worktrees.

This module answers the *location* question alone: which worktree, if any,
contains a path, and which repository that worktree belongs to. It holds the
pure path arithmetic, the marker walk, and the agreement rule between an
inherited ``GIT_DIR``/``GIT_WORK_TREE`` and the worktree the marker walk found.
The Git queries those rules need arrive through :class:`GitLocationQuery`, so
this unit reads no environment and launches no process of its own.

The answers are tri-state. ``True`` and ``False`` are definite, and ``None``
means the location is unknown, which every caller treats as "preserve" because
acting on an unknown location could change the hook's decision.
"""

from __future__ import annotations

import dataclasses
import os
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dataclasses.dataclass(slots=True, frozen=True)
class RepositoryIdentity:
    """The repository one worktree belongs to.

    Attributes
    ----------
    git_dir
        Absolute git directory Git itself reported. Every index and ignore
        query is pinned to it, so a relative ``GIT_DIR`` cannot select a
        different repository from another working directory.
    top
        Absolute worktree top Git itself reported.

    """

    git_dir: Path
    top: Path


class GitLocationQuery(typ.Protocol):
    """The Git and environment answers the location rules are decided from.

    An implementation reads one explicit environment mapping and runs Git
    through an injected runner; the rules in this module and in
    :mod:`post_turn_quality_stop_hook.git_location` see only these questions.
    """

    def names_explicit_repository(self) -> bool:
        """Whether the environment names a repository without a marker.

        A worktree configured only through ``GIT_DIR`` and ``GIT_WORK_TREE``
        carries no ``.git`` marker, so the marker walk cannot tell "outside
        every worktree" from "configured by the environment".
        """
        ...

    def configured_identity(self, start: Path) -> RepositoryIdentity | None:
        """Return the repository the environment names, or ``None``.

        The answer is read with ``GIT_DIR`` and ``GIT_WORK_TREE`` present.
        """
        ...

    def discovered_identity(self, start: Path) -> RepositoryIdentity | None:
        """Return the repository the directory itself belongs to, or ``None``.

        The answer is read with ``GIT_DIR``, ``GIT_WORK_TREE``, and any
        inherited ``GIT_INDEX_FILE`` removed, so it describes the checkout the
        marker walk found rather than one the environment configured.
        """
        ...

    def tracked_file(self, identity: RepositoryIdentity, relative: Path) -> bool | None:
        """Answer whether ``relative`` is in the index of ``identity``."""
        ...

    def ignored_entry(
        self, identity: RepositoryIdentity, relative: Path
    ) -> bool | None:
        """Answer whether an ignore rule matches ``relative`` in ``identity``."""
        ...


def lexical_relative(candidate: Path, repo: Path) -> Path | None:
    """Return ``candidate`` relative to ``repo`` without touching the filesystem."""
    try:
        relative = Path(os.path.relpath(candidate, repo))
    except ValueError:
        return None
    if relative.parts and relative.parts[0] == "..":
        return None
    return relative


def worktree_relative(path: Path, root: Path) -> Path | None:
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
        return lexical_relative(path, root)
    return relative


def entry_relative(path: Path, root: Path) -> Path | None:
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
        return lexical_relative(path, root)


def nearest_existing(path: Path) -> Path:
    """Return ``path`` or its nearest existing ancestor.

    Repository discovery needs a directory that exists. A tracked file whose
    parent directory was deleted still has an index entry, and its nearest
    existing ancestor is the first directory Git can be asked about.
    """
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def has_git_ancestor(start: Path) -> bool | None:
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


def locate(
    path: Path, query: GitLocationQuery
) -> tuple[RepositoryIdentity | None, bool | None]:
    """Return the repository around ``path`` and the marker answer.

    A worktree named only through ``GIT_DIR`` and ``GIT_WORK_TREE`` holds no
    ``.git`` marker for the ancestor walk to find. In that configuration a
    missing marker is inconclusive, so Git is asked directly; a query that
    still cannot answer leaves the location unknown rather than outside every
    worktree, because callers must preserve files on an unknown location.

    When the marker walk does find a worktree, an inherited ``GIT_DIR`` or
    ``GIT_WORK_TREE`` must agree with it. If they disagree, the index and
    ignore queries would answer from another repository, so the location is
    unknown and the file is preserved.

    Returns
    -------
    tuple[RepositoryIdentity | None, bool | None]
        The repository identity, and ``True`` when a worktree was found. When
        the second item is ``False`` the path is outside every worktree, and
        ``None`` means the location itself is unknown. A ``True`` with a
        ``None`` identity means the marker exists but Git could not report the
        repository, so callers must treat it as unanswerable rather than
        absent.

    """
    start = nearest_existing(path.parent)
    has_ancestor = has_git_ancestor(start)
    if has_ancestor is True:
        if _environment_disagrees_with_marker(start, query):
            return None, None
        return query.discovered_identity(start), True
    if has_ancestor is False and query.names_explicit_repository():
        # Git reads these variables itself, so only its answer is authoritative.
        return query.configured_identity(start), True
    return None, has_ancestor


def _environment_disagrees_with_marker(start: Path, query: GitLocationQuery) -> bool:
    """Whether an inherited repository names a different one from the marker's.

    Comparing both the git directory and the worktree top means neither a
    different index nor a different checkout can pass as the marker's
    repository. An answer Git cannot give is not agreement, so the caller
    treats it as unknown.
    """
    if not query.names_explicit_repository():
        return False
    configured = query.configured_identity(start)
    if configured is None:
        return True
    return configured != query.discovered_identity(start)


def explicit_repository_names(
    environ: cabc.Mapping[str, str], names: cabc.Iterable[str]
) -> bool:
    """Return whether ``environ`` sets any of ``names`` to a non-empty value."""
    return any(environ.get(name) for name in names)
