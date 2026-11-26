"""Answer whether Git could report a path as a change, or track it.

The decision log may only write or delete a file where no hook decision can
observe it. Answering that needs three questions about a path: whether a
worktree contains it, whether Git tracks it, and whether an ignore rule matches
it. Each answer is tri-state: ``True`` and ``False`` are Git's own answers, and
``None`` means Git could not answer, which every caller treats as "preserve"
because acting on an unknown location could change the hook's decision.

The module is split by responsibility:

- :mod:`post_turn_quality_stop_hook.git_location_paths` holds the path
  arithmetic and the rules that decide which worktree a path belongs to;
- :mod:`post_turn_quality_stop_hook.git_location_query` binds those rules to
  Git, running every index and ignore query against a pinned absolute git
  directory;
- this module turns the two into the tri-state policy answers the decision log
  asks for, and reads no environment and launches no process itself.

Callers that write or delete a decision-log file use the
:class:`GitLocationQueries` value these helpers are composed into; the bindings
live in :mod:`post_turn_quality_stop_hook.decision_log.composition`, so no
adapter constructor holds a silent default.
"""

from __future__ import annotations

import dataclasses
import typing as typ

from post_turn_quality_stop_hook import git_location_paths as paths

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.git_location_paths import (
        GitLocationQuery,
        RepositoryIdentity,
    )

__all__ = [
    "GitLocationPaths",
    "GitLocationQueries",
    "is_ignored",
    "is_in_worktree",
    "is_tracked",
    "is_tracked_entry",
    "queries_for",
    "would_appear_as_change",
]


@dataclasses.dataclass(slots=True, frozen=True)
class GitLocationPaths:
    """The Git answers one path-location decision was made from.

    Attributes
    ----------
    identity
        Repository the path belongs to, or ``None`` when it is outside every
        worktree or the location is unknown.
    located
        ``True`` when a worktree contains the path, ``False`` when none does,
        and ``None`` when that could not be established.

    """

    identity: RepositoryIdentity | None
    located: bool | None


def _paths_for(path: Path, query: GitLocationQuery) -> GitLocationPaths:
    """Locate ``path`` through ``query`` and return both halves of the answer."""
    identity, located = paths.locate(path, query)
    return GitLocationPaths(identity=identity, located=located)


def is_tracked(  # ruff:ignore[too-many-return-statements]  # each unknown answer has its own cause.
    path: Path, query: GitLocationQuery
) -> bool | None:
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
    query
        Git and environment answers the location is decided from.

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
    found = _paths_for(path, query)
    if found.located is True:
        if found.identity is None:
            return None
        return _tracked_relative(path, found.identity, query)
    if found.located is None:
        return None
    # No worktree contains the path lexically. It may still be a link into one,
    # so the target's own location decides. A link that cannot be resolved
    # leaves the answer unknown rather than reading as outside every worktree.
    target = _resolved_target(path)
    if target is None:
        return None
    target_paths = _paths_for(target, query)
    if target_paths.located is not True:
        return target_paths.located
    if target_paths.identity is None:
        return None
    return _tracked_relative(target, target_paths.identity, query)


def _tracked_relative(
    path: Path, identity: RepositoryIdentity, query: GitLocationQuery
) -> bool | None:
    """Answer whether ``path`` is tracked in the repository of ``identity``."""
    relative = paths.worktree_relative(path, identity.top)
    if relative is None:
        # Git found a worktree, but neither the lexical nor the resolved path
        # lies inside it. Refusing is the only answer that cannot change the
        # hook's decision.
        return True
    return query.tracked_file(identity, relative)


def _resolved_target(path: Path) -> Path | None:
    """Return ``path`` with every symbolic link resolved, or ``None``.

    ``None`` means a link on the path could not be resolved, so no location
    claim can be made about the file.
    """
    try:
        return path.resolve()
    except OSError:
        return None


def is_tracked_entry(path: Path, query: GitLocationQuery) -> bool | None:
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
    query
        Git and environment answers the location is decided from.

    Returns
    -------
    bool | None
        ``True`` when the entry is in the index, ``False`` when the path is
        outside every worktree or Git confirms the entry is absent from it, and
        ``None`` when Git could not answer. Callers must preserve the entry on
        anything but a confirmed ``False``.

    """
    found = _paths_for(path, query)
    if found.located is not True:
        return found.located
    if found.identity is None:
        return None
    relative = paths.entry_relative(path, found.identity.top)
    if relative is None:
        return True
    return query.tracked_file(found.identity, relative)


def is_in_worktree(path: Path, query: GitLocationQuery) -> bool | None:
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
    query
        Git and environment answers the location is decided from.

    Returns
    -------
    bool | None
        ``True`` when Git resolves a worktree around the path, ``False`` when
        it is outside every worktree, and ``None`` when Git could not answer
        or a symbolic link on the path could not be resolved.

    """
    found = _paths_for(path, query)
    if found.located is not True:
        return found.located
    if found.identity is None:
        # The marker walk found a worktree, but Git could not report its
        # repository. The location is unanswerable, not "outside every one".
        return None
    # A symlink that escapes the worktree still answers "preserve", which is
    # the safe direction: retention must never delete on a doubtful answer.
    return True


def is_ignored(path: Path, query: GitLocationQuery) -> bool | None:
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
    query
        Git and environment answers the location is decided from.

    Returns
    -------
    bool | None
        ``True`` when an ignore rule matches, ``False`` when Git reports no
        match or the path is outside every worktree, and ``None`` when Git
        could not answer. Only ``True`` lets retention delete, so every other
        answer preserves the file.

    """
    found = _paths_for(path, query)
    if found.located is not True:
        return found.located
    if found.identity is None:
        return None
    relative = paths.entry_relative(path, found.identity.top)
    if relative is None:
        # No route into the worktree exists, so no ignore rule can match the
        # path either. ``False`` preserves the file, which is the safe answer.
        return False
    return query.ignored_entry(found.identity, relative)


def would_appear_as_change(path: Path, query: GitLocationQuery) -> bool | None:
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
    query
        Git and environment answers the location is decided from.

    Returns
    -------
    bool | None
        ``True`` when a worktree would report the path, ``False`` when no
        worktree contains it or Git ignores it, and ``None`` when Git could
        not answer. Only ``False`` permits a write; every other answer refuses
        it, because a refused log cannot change a decision.

    """
    located = is_in_worktree(path, query)
    if located is not True:
        # ``False`` means no worktree can report the path. ``None`` is not
        # that case and must stay unknown, so the caller refuses the write.
        return located
    ignored = is_ignored(path, query)
    if ignored is True:
        return False
    if ignored is False:
        return True
    return None


@dataclasses.dataclass(slots=True, frozen=True)
class GitLocationQueries:
    """The four ownership questions retention and the sink ask, already bound.

    Attributes
    ----------
    is_tracked
        Answers whether Git tracks a file, following symbolic links.
    is_tracked_entry
        Answers whether the directory entry itself is tracked.
    in_worktree
        Answers whether any worktree contains a file.
    is_ignored
        Answers whether an ignore rule matches the directory entry.
    is_visible
        Answers whether a worktree could report the file as a change.

    """

    is_tracked: typ.Callable[[Path], bool | None]
    is_tracked_entry: typ.Callable[[Path], bool | None]
    in_worktree: typ.Callable[[Path], bool | None]
    is_ignored: typ.Callable[[Path], bool | None]
    is_visible: typ.Callable[[Path], bool | None]


def queries_for(query: GitLocationQuery) -> GitLocationQueries:
    """Bind ``query`` into the callables the decision log consumes."""
    return GitLocationQueries(
        is_tracked=lambda path: is_tracked(path, query),
        is_tracked_entry=lambda path: is_tracked_entry(path, query),
        in_worktree=lambda path: is_in_worktree(path, query),
        is_ignored=lambda path: is_ignored(path, query),
        is_visible=lambda path: would_appear_as_change(path, query),
    )
