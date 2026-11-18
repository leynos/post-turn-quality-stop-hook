"""Which daily files this run may write, prune, or exclude.

Appending to a file the hook does not own would mix two writers into one line
stream, and excluding one would hide repository content from the gates. This
policy is the single place that decides ownership, so the sink, retention, and
the exclusion queries cannot disagree about it.

Ownership is settled from the file's own records under the exclusive lock,
never from the filename alone. An empty file proves nothing: it may be a file
the user created at today's path, and hiding it from Git would let a
logging-enabled run pass a gate that a logging-disabled run blocks on.
"""

from __future__ import annotations

import os
import typing as typ

from post_turn_quality_stop_hook.decision_log.ownership import (
    _is_hook_record_descriptor,
)
from post_turn_quality_stop_hook.decision_log.records import _warn
from post_turn_quality_stop_hook.git import ExcludedPath

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


class WritePolicy:
    """Decide whether this run may append to, and exclude, a daily file.

    Parameters
    ----------
    on_error
        Callback for refusals. It receives a fixed message with no path and no
        exception text.
    is_tracked
        Git ownership query. A daily file Git tracks is never appended to,
        because that write would make a clean worktree dirty before the
        uncommitted-changes gate runs and could change the decision. The same
        refusal applies when the query cannot answer. Once the policy refuses,
        it stays inert for the rest of the run, including retention.

    """

    def __init__(
        self,
        *,
        on_error: cabc.Callable[[str], None] = _warn,
        is_tracked: cabc.Callable[[Path], bool | None] | None = None,
    ) -> None:
        """Store the error sink and the Git ownership query."""
        self._on_error = on_error
        self._is_tracked = is_tracked
        # Exclusions are resolved when the file is created, not when they are
        # queried. A symbolic link that cannot be resolved later would
        # otherwise strand the exclusion outside the worktree and let the run's
        # own log file change the decision.
        self._exclusions: dict[Path, ExcludedPath] = {}
        self._directories: dict[Path, Path] = {}
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

    def resolve_directory(self, path: Path) -> Path | None:
        """Return the physical directory for ``path``, or ``None`` if unknown.

        The exclusion must name the physical directory, because Git reports
        physical paths. Without it this run's own log could become visible to
        the gates, so the caller refuses the record before anything is created.
        """
        try:
            resolved = path.parent.resolve()
        except OSError:
            self._refuse("could not resolve the decision log directory")
            return None
        self._directories[path] = resolved
        return resolved

    def permits_write(self, path: Path) -> bool:
        """Return whether Git permits appending to ``path`` right now.

        The query runs before every append. A cached answer would let a file
        that another process staged mid-run receive records, turning a clean
        worktree dirty while the hook is evaluating it.
        """
        if self._refused:
            return False
        if self._is_tracked is None:
            return True
        try:
            tracked = self._is_tracked(path)
        except Exception:  # ruff:ignore[blind-except]  # an unknown answer must not risk the decision.
            tracked = None
        if tracked is False:
            return True
        self._refuse(
            "the decision log file is tracked by Git"
            if tracked
            else "could not determine whether the decision log file is tracked by Git"
        )
        return False

    def created(self, path: Path) -> None:
        """Record the exclusion for a file this run has just created.

        Called before the lock is attempted, so a run that creates the file and
        then loses the lock race still hides the empty file it made instead of
        leaving it as a new untracked change.
        """
        self._exclude(path)

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
            # The directory entry no longer holds the file whose claim was
            # cached, so the exclusion for that path must not survive: it would
            # hide whatever now sits there from the gates.
            self._exclusions.pop(path, None)
            self._refuse("the decision log file was not written by this hook")
            return False
        self._remember(path, identity)
        return True

    def excluded_paths(self) -> tuple[ExcludedPath, ...]:
        """Return the daily files this run wrote to, for repository queries.

        Every file this run appended to is listed, including a file an earlier
        invocation created once this run has verified that it consists only of
        hook records. Both kinds of file exist only because logging is enabled,
        so hiding both is what makes a logging-enabled run report the same
        worktree as a logging-disabled one. A file the hook does not own is
        never listed. The set grows during the run, which also covers a run
        that crosses midnight and writes a second daily file.

        Each entry names the directory's physical path, captured when the file
        was created or claimed. Resolving then keeps the exclusion matchable
        even if the symbolic link that led to it cannot be resolved later.
        """
        return tuple(self._exclusions[path] for path in sorted(self._exclusions))

    def _remember(self, path: Path, identity: tuple[int, int] | None) -> None:
        """Record that ``path`` may receive records, and exclude it."""
        if identity is not None:
            self._claims[path] = identity
        self._exclude(path)

    def _exclude(self, path: Path) -> None:
        """Add ``path`` to the exclusions when its directory is known."""
        directory = self._directories.get(path)
        if directory is None:
            try:
                directory = path.parent.resolve()
            except OSError:
                return
            self._directories[path] = directory
        self._exclusions.setdefault(path, ExcludedPath(directory, path.name))

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
