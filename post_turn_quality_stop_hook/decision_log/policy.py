"""Which daily files this run may write or prune.

Appending to a file the hook does not own would mix two writers into one line
stream, and writing to a file the gates can see would let the log change the
decision. This policy is the single place that decides both, so the sink and
retention cannot disagree about it.

Ownership is settled from the file's own records under the exclusive lock,
never from the filename alone. An empty file proves nothing: it may be a file
the user created at today's path.

Location is settled before the file is created. Logging must never change the
decision, and the uncommitted-changes gate reports untracked entries, so a
daily file may only be written where that gate cannot see it. Anything else is
refused for the rest of the run, and nothing is created or deleted.
"""

from __future__ import annotations

import os
import typing as typ

from post_turn_quality_stop_hook.decision_log.ownership import (
    _is_hook_record_descriptor,
)
from post_turn_quality_stop_hook.decision_log.records import _warn
from post_turn_quality_stop_hook.git import is_tracked as git_is_tracked
from post_turn_quality_stop_hook.git import would_appear_as_change

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_TRACKED_MESSAGE = "the decision log file is tracked by Git"
_UNKNOWN_TRACKED_MESSAGE = (
    "could not determine whether the decision log file is tracked by Git"
)
_VISIBLE_MESSAGE = "the decision log location could appear as a worktree change"
_UNKNOWN_VISIBLE_MESSAGE = (
    "could not determine whether the decision log location can appear as a"
    " worktree change"
)


class WritePolicy:
    """Decide whether this run may append to, or prune, a daily file.

    Parameters
    ----------
    on_error
        Callback for refusals. It receives a fixed message with no path and no
        exception text.
    is_tracked
        Git ownership query, defaulting to
        :func:`post_turn_quality_stop_hook.git.is_tracked`. A daily file Git
        tracks is never appended to, because that write would make a clean
        worktree dirty before the uncommitted-changes gate runs and could
        change the decision. The same refusal applies when the query cannot
        answer.
    is_visible
        Query for whether a worktree could report the file as an untracked
        change, defaulting to
        :func:`post_turn_quality_stop_hook.git.would_appear_as_change`. Only a
        definite ``False`` permits a write; a visible path, or an answer Git
        cannot give, is refused. Once the policy refuses, it stays inert for
        the rest of the run, including retention.

    """

    def __init__(
        self,
        *,
        on_error: cabc.Callable[[str], None] = _warn,
        is_tracked: cabc.Callable[[Path], bool | None] = git_is_tracked,
        is_visible: cabc.Callable[[Path], bool | None] = would_appear_as_change,
    ) -> None:
        """Store the error sink and the Git queries."""
        self._on_error = on_error
        self._is_tracked = is_tracked
        self._is_visible = is_visible
        # Visibility is a property of the path, so it is asked once per file
        # and the answer is reused. The cache is keyed by path because a run
        # that crosses midnight writes a second daily file.
        self._visibility: dict[Path, bool | None] = {}
        # The identity of each file this run has established it may append to.
        # A cached answer is reused only while the open descriptor still names
        # that same file, so a replaced directory entry is re-checked rather
        # than trusted because its path matched.
        self._claims: dict[Path, tuple[int, int]] = {}
        self._refused = False

    @property
    def refused(self) -> bool:
        """Whether the policy has refused all further storage work."""
        return self._refused

    def permits_write(self, path: Path) -> bool:
        """Return whether this run may write to ``path`` right now.

        A tracked file is refused first: writing to it would make a clean
        worktree dirty. The location question comes next, and only a definite
        "no worktree can report this path" permits the write. The tracking
        query runs before every append rather than once, because a file another
        process staged mid-run must not receive records.
        """
        if self._refused:
            return False
        tracked = self._query(self._is_tracked, path)
        if tracked is not False:
            self._refuse(_TRACKED_MESSAGE if tracked else _UNKNOWN_TRACKED_MESSAGE)
            return False
        visible = self._visibility.get(path)
        if path not in self._visibility:
            visible = self._query(self._is_visible, path)
            self._visibility[path] = visible
        if visible is not False:
            self._refuse(_VISIBLE_MESSAGE if visible else _UNKNOWN_VISIBLE_MESSAGE)
            return False
        return True

    def may_append(self, path: Path, descriptor: int, *, created: bool) -> bool:
        """Return whether the locked file may receive this run's records.

        The caller holds an exclusive lock, so no other hook process can be
        midway through its first append. A file this run created is ours by
        definition, and a file this run already established stays ours while
        the descriptor still identifies it. Any other file must consist of at
        least one hook record and nothing else: an empty file proves nothing,
        and a file that only begins with a hook record is not ours either.
        """
        if self._refused:
            return False
        identity = _identity(descriptor)
        still_claimed = identity is not None and self._claims.get(path) == identity
        if created or still_claimed:
            self._remember(path, identity)
            return True
        try:
            owned = identity is not None and _is_hook_record_descriptor(descriptor)
        except OSError:  # an unreadable file cannot establish ownership
            owned = False
        if not owned:
            self._refuse("the decision log file was not written by this hook")
            return False
        self._remember(path, identity)
        return True

    @staticmethod
    def _query(query: cabc.Callable[[Path], bool | None], path: Path) -> bool | None:
        """Ask one Git query, treating an exception as an unknown answer.

        An exception must not escape into the hook: the decision is already
        being computed, and an unknown answer already refuses the write.
        """
        try:
            return query(path)
        except Exception:  # ruff:ignore[blind-except]  # an unknown answer must not risk the decision.
            return None

    def _remember(self, path: Path, identity: tuple[int, int] | None) -> None:
        """Record that ``path`` may receive records."""
        if identity is not None:
            self._claims[path] = identity

    def _refuse(self, message: str) -> None:
        """Refuse the rest of the run, reporting the fixed reason once."""
        if self._refused:
            return
        self._refused = True
        self._on_error(f"{message}; not writing to it")


def _identity(descriptor: int) -> tuple[int, int] | None:
    """Return the device and inode identifying an open file, if available."""
    try:
        stat_result = os.fstat(descriptor)
    except OSError:
        return None
    return (stat_result.st_dev, stat_result.st_ino)
