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

The predicates below are read-only queries: they answer whether this run may
use a file and carry the refusal reason on that answer. Recording the refusal
is a separate command, :meth:`WritePolicy.refuse_run`, which the sink invokes
once it acts on a refusal. Keeping the two apart means asking a question can
never write a warning or change the run's state.

The one method that does write instance state is
:meth:`WritePolicy.settle_visibility`, whose name and docstring say so: it
caches a path's visibility answer so later appends in the same run reuse it.
Every other public method here is a query in the sense above.
"""

from __future__ import annotations

import dataclasses
import enum
import os
import typing as typ

from post_turn_quality_stop_hook.decision_log.ownership import (
    _is_hook_record_descriptor,
)
from post_turn_quality_stop_hook.decision_log.records import _warn

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


class RefusalReason(enum.StrEnum):
    """Why this run may not use a decision-log file.

    Each value renders as the fixed, path-free message the run reports once,
    so no caller has to compose refusal text.
    """

    FILE_TRACKED = "the decision log file is tracked by Git"
    TRACKING_UNKNOWN = (
        "could not determine whether the decision log file is tracked by Git"
    )
    LOCATION_VISIBLE = "the decision log location could appear as a worktree change"
    VISIBILITY_UNKNOWN = (
        "could not determine whether the decision log location can appear as a"
        " worktree change"
    )
    NOT_HOOK_OWNED = "the decision log file was not written by this hook"


@dataclasses.dataclass(slots=True, frozen=True)
class WriteVerdict:
    """A predicate's answer, carrying the reason when the answer is no.

    Attributes
    ----------
    permitted
        Whether the queried operation may proceed now.
    refusal
        Why it may not, ``None`` when it may, and also ``None`` when the run
        had already refused this file. That last case is not a new refusal, so
        the caller records nothing; only a verdict carrying a reason reaches
        :meth:`WritePolicy.refuse_run`.

    """

    permitted: bool
    refusal: RefusalReason | None = None

    @classmethod
    def allow(cls) -> WriteVerdict:
        """Return a permitting answer."""
        return cls(permitted=True)

    @classmethod
    def denied(cls, refusal: RefusalReason) -> WriteVerdict:
        """Return a refusing answer carrying its reason."""
        return cls(permitted=False, refusal=refusal)

    @classmethod
    def already_refused(cls) -> WriteVerdict:
        """Return a refusing answer for a run that has already refused."""
        return cls(permitted=False)


class WritePolicy:
    """Decide whether this run may append to, or prune, a daily file.

    Parameters
    ----------
    on_error
        Callback for refusals. It receives a fixed message with no path and no
        exception text.
    is_tracked
        Git ownership query. A daily file Git tracks is never appended to,
        because that write would make a clean worktree dirty before the
        uncommitted-changes gate runs and could change the decision. The same
        refusal applies when the query cannot answer.
    is_visible
        Query for whether a worktree could report the file as an untracked
        change. Only a definite ``False`` permits a write; a visible path, or
        an answer Git cannot give, is refused. Once the policy refuses, it
        stays inert for the rest of the run, including retention.

    """

    def __init__(
        self,
        *,
        on_error: cabc.Callable[[str], None] = _warn,
        is_tracked: cabc.Callable[[Path], bool | None],
        is_visible: cabc.Callable[[Path], bool | None],
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

    def permits_write(self, path: Path) -> WriteVerdict:
        """Answer whether this run may write to ``path`` right now.

        A tracked file is refused first: writing to it would make a clean
        worktree dirty. The location question comes next, and only a definite
        "no worktree can report this path" permits the write. The tracking
        query runs before every append rather than once, because a file another
        process staged mid-run must not receive records.

        This is a read-only query: it asks Git afresh on every call and writes
        no instance state. A caller that wants the visibility answer reused for
        later appends to the same file calls :meth:`settle_visibility`, whose
        name says that it caches. The caller records a refusal by invoking
        :meth:`refuse_run` with the returned reason.
        """
        if self._refused:
            return WriteVerdict.already_refused()
        tracked = self._query(self._is_tracked, path)
        if tracked is not False:
            return WriteVerdict.denied(
                RefusalReason.FILE_TRACKED
                if tracked
                else RefusalReason.TRACKING_UNKNOWN
            )
        return _visibility_verdict(visible=self._query(self._is_visible, path))

    def settle_visibility(self, path: Path) -> WriteVerdict:
        """Ask the visibility question once for ``path`` and cache the answer.

        This method **writes the policy's per-path visibility cache**, which is
        why it is named for the act of settling rather than for a query. The
        sink calls it for the first append to each daily file so later records
        in the same run reuse one Git answer instead of repeating the call.

        A cached answer describes the pathname, not the file. Creating a daily
        file is the one append that can add a worktree entry, so
        :meth:`may_create` deliberately asks again through the directory that
        will receive the file rather than trusting this cache.

        A refusal is returned, not recorded; :meth:`refuse_run` is the command
        that changes the run's state.
        """
        if self._refused:
            return WriteVerdict.already_refused()
        tracked = self._query(self._is_tracked, path)
        if tracked is not False:
            return WriteVerdict.denied(
                RefusalReason.FILE_TRACKED
                if tracked
                else RefusalReason.TRACKING_UNKNOWN
            )
        if path not in self._visibility:
            self._visibility[path] = self._query(self._is_visible, path)
        return _visibility_verdict(visible=self._visibility[path])

    def may_create(self, location: Path) -> WriteVerdict:
        """Answer whether a daily file may be created at ``location``.

        ``location`` is anchored to the directory that will receive the file,
        so the answer describes that physical directory rather than a pathname
        that could be redirected again. Unlike :meth:`settle_visibility`, this
        asks the visibility question afresh every time and caches nothing: the
        earlier answer was about the pathname, and the directory or the rule
        that ignored it may have changed since. Creating the file is the one
        append that can add a worktree change, so it is the one that must not
        reuse a cached answer.

        This is a read-only query; :meth:`refuse_run` records a refusal.
        """
        if self._refused:
            return WriteVerdict.already_refused()
        return _visibility_verdict(visible=self._query(self._is_visible, location))

    def claim_append(
        self, path: Path, descriptor: int, *, created: bool, location: Path
    ) -> bool:
        """Claim the locked file for this run's records, or refuse to.

        The caller holds an exclusive lock, so no other hook process can be
        midway through its first append. A file this run created is ours by
        definition: its creation was already checked through the directory that
        received it. Any other file is checked again here, at the physical
        location ``location`` names after the log directory was opened, because
        a pathname checked earlier could since have been redirected. A file Git
        tracks there is refused: appending to it would make a clean worktree
        dirty before the uncommitted-changes gate runs. Then the file must
        consist of at least one hook record and nothing else: an empty file
        proves nothing, and a file that only begins with a hook record is not
        ours either.

        A refusal is recorded through :meth:`refuse_run`; the caller then
        writes nothing.
        """
        if self._refused:
            return False
        identity = _identity(descriptor)
        still_claimed = identity is not None and self._claims.get(path) == identity
        # The tracking question is asked about the physical location, after the
        # directory was opened, on every append. It is asked even for a file
        # this run already claimed: unlike the record-shape check, the tracking
        # answer describes a worktree state a concurrent process can change, so
        # reusing the earlier answer could append to a file that has since been
        # staged and dirty the tree before the gate runs.
        tracked = self._query(self._is_tracked, location)
        if tracked is not False:
            self.refuse_run(
                RefusalReason.FILE_TRACKED
                if tracked
                else RefusalReason.TRACKING_UNKNOWN
            )
            return False
        if created or still_claimed:
            self._remember(path, identity)
            return True
        try:
            owned = identity is not None and _is_hook_record_descriptor(descriptor)
        except OSError:  # an unreadable file cannot establish ownership
            owned = False
        if not owned:
            self.refuse_run(RefusalReason.NOT_HOOK_OWNED)
            return False
        self._remember(path, identity)
        return True

    def refuse_run(self, reason: RefusalReason) -> None:
        """Refuse all further storage work, reporting the fixed reason once.

        This is the command that changes state. Every predicate above is
        read-only, so the warning is emitted exactly when the caller acts on a
        refusal, and exactly once per run however often a refusal is acted on.
        """
        if self._refused:
            return
        self._refused = True
        self._on_error(f"{reason.value}; not writing to it")

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


def _visibility_verdict(*, visible: bool | None) -> WriteVerdict:
    """Turn one visibility answer into a write verdict.

    Only a definite "no worktree can report this path" permits a write. A
    visible path, and an answer Git could not give, both refuse it. Keeping
    this a pure function means both the cached and the fresh answer are judged
    by exactly the same rule.
    """
    if visible is not False:
        return WriteVerdict.denied(
            RefusalReason.LOCATION_VISIBLE
            if visible
            else RefusalReason.VISIBILITY_UNKNOWN
        )
    return WriteVerdict.allow()


def _identity(descriptor: int) -> tuple[int, int] | None:
    """Return the device and inode identifying an open file, if available."""
    try:
        stat_result = os.fstat(descriptor)
    except OSError:
        return None
    return (stat_result.st_dev, stat_result.st_ino)
