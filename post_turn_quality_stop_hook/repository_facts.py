"""Best-effort observation of repository facts for the decision log.

These facts exist only to explain a decision in the durable record, so a query
that cannot run must never change what the hook decides. The collection lives
in one place and reports through the observer boundary; when a Git query cannot
even start, the failure itself is reported as a bounded, path-free fact instead
of aborting the run or passing silently.

The queries arrive through :class:`RepositoryFactsSource`, a port the
composition root implements with the real Git plumbing. This module therefore
imports no Git module and no subprocess wrapper: it decides what to record and
when to withhold a value, and the adapter decides how each answer is read. A
test supplies a prepared source and needs no repository at all.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.git_facts import GitFacts
    from post_turn_quality_stop_hook.observer import PipelineObserver

#: Fixed description of a repository observation that could not run. The
#: underlying ``OSError`` may carry a filesystem path, so only this text is
#: recorded. Reporting it keeps a diagnostic that used to be swallowed.
OBSERVATION_FAILED = "could not observe repository facts"


class RepositoryFactsSource(typ.Protocol):
    """Read the log-only facts one observation reports.

    Every method is a query that may fail: ``None`` means the fact is unknown,
    never that it is ``False`` or empty. The observation treats an unknown
    answer as a reason to withhold that fact, so a failed read cannot be
    mistaken for a negative result.
    """

    def current_branch(self, repo: Path) -> str | None:
        """Return the checked-out branch, or ``None`` for a detached ``HEAD``."""
        ...

    def head_commit(self, repo: Path) -> str | None:
        """Return the current ``HEAD`` commit, or ``None`` when unreadable."""
        ...

    def has_uncommitted_changes(self, repo: Path) -> bool | None:
        """Return whether the worktree is dirty, or ``None`` when unreadable."""
        ...

    def is_ahead_of_upstream(self, repo: Path, upstream_ref: str) -> bool | None:
        """Return whether ``HEAD`` is ahead of ``upstream_ref``, or ``None``."""
        ...


def _unpushed_for_checkout(  # ruff:ignore[too-many-arguments,too-many-positional-arguments]  # source, checkout and record facts are distinct inputs.
    source: RepositoryFactsSource,
    repo: Path,
    branch: str | None,
    head: str | None,
    facts: GitFacts | None,
) -> bool | None:
    """Return whether ``HEAD`` is ahead of the snapshot's upstream.

    The upstream ref was captured for the branch recorded in ``facts``. If the
    checkout has since moved to another branch, comparing this ``HEAD`` with
    that upstream would count another branch's commits, so the answer is
    withheld rather than reported against the wrong branch. A snapshot taken
    on a detached ``HEAD`` has no upstream to compare, and the adapter applies
    the same liveness rule.

    The unpushed query compares the live ``HEAD`` with the upstream, so the
    branch and commit are read again after it. If either has moved since the
    facts the record will carry were read, the count describes a different
    checkout; it is withheld rather than reported beside the wrong branch or
    commit. This keeps the answer bound to the ``branch`` and ``head`` the
    caller records, and it stays best-effort: a withheld count never changes a
    gate decision, an exit code, or standard output.
    """
    if facts is None or facts.upstream_ref is None:
        return None
    if facts.branch is not None and branch != facts.branch:
        return None
    unpushed = source.is_ahead_of_upstream(repo, facts.upstream_ref)
    if unpushed is None:
        return None
    after_branch = source.current_branch(repo)
    after_head = source.head_commit(repo)
    if after_branch != branch or after_head != head:
        return None
    return unpushed


def observe_repository_facts(  # ruff:ignore[too-many-arguments,too-many-positional-arguments]  # observer, source, repository and record facts are distinct inputs.
    observer: PipelineObserver,
    source: RepositoryFactsSource,
    repo: Path,
    base_ref: str,
    facts: GitFacts | None,
) -> None:
    """Query and report the best-effort repository facts for one run.

    Parameters
    ----------
    observer
        Receives the observed facts, or the bounded failure description.
    source
        Query port supplying each fact. The composition root supplies the real
        Git adapter; a test supplies a prepared one.
    repo
        Repository root path.
    base_ref
        Base ref the run compares against.
    facts
        Collected Git facts. The upstream ref is read from here rather than
        re-read from configuration.

    Notes
    -----
    A source may report a failed query as ``None``, but starting a subprocess
    can still raise ``OSError``. That error is recorded as a bounded failure
    and execution continues: none of these facts participates in the hook's
    decision.

    """
    # A no-op observer would discard every fact, so asking for them would only
    # add work to a run with logging disabled.
    if not observer.records_repository_facts():
        return
    try:
        branch = source.current_branch(repo)
        head = source.head_commit(repo)
        dirty = source.has_uncommitted_changes(repo)
        unpushed = _unpushed_for_checkout(source, repo, branch, head, facts)
    except OSError:
        observer.repository_observed({
            "repo_root": str(repo),
            "base_ref": base_ref,
            "error": OBSERVATION_FAILED,
        })
        return
    # Query these again rather than reuse an earlier stage's facts: the
    # quality checks run between preparation and the branch-state gates, so
    # each caller needs the repository state at its own point in time.
    observer.repository_observed({
        "repo_root": str(repo),
        "branch": branch,
        "head": head,
        "base_ref": base_ref,
        "dirty": dirty,
        "unpushed": unpushed,
    })
