"""The Git adapter behind the location rules.

Every question :class:`~post_turn_quality_stop_hook.git_location_paths.\
GitLocationQuery` asks is answered here: whether the environment names a
repository, which repository two different views resolve to, and whether an
index or ignore rule matches one file. The adapter holds the environment
mapping and the process runner it was built with, so the rules above it read no
``os.environ`` and no module-level ``run``.

Each index and ignore query is pinned to an absolute git directory, so a
relative ``GIT_DIR`` cannot resolve against a different working directory and
select another repository. ``GIT_INDEX_FILE`` is removed from the inherited
environment, so an index another process configured cannot answer for this
one. Both values come from the caller's snapshot rather than the live process
environment, so a value the process changes mid-run cannot split one query
from another.
"""

from __future__ import annotations

import dataclasses
import typing as typ
from pathlib import Path

from post_turn_quality_stop_hook.git_location_paths import (
    RepositoryIdentity,
    explicit_repository_names,
)
from post_turn_quality_stop_hook.process import LaunchedProcess, run

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import subprocess

    #: Runs one Git command in a directory, with the named variables removed
    #: from the supplied environment.
    GitRunner = cabc.Callable[..., subprocess.CompletedProcess[str]]

#: Environment variables that name a repository Git would otherwise discover
#: only through a ``.git`` marker. When the marker walk already found a
#: worktree, a value here that names a different repository makes the location
#: unknown rather than letting one repository's index answer for the other.
_EXPLICIT_REPOSITORY_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE")

#: Environment variable naming an index file to read instead of the
#: worktree's own. It is cleared for the location queries only: an inherited
#: value would make ``ls-files`` and ``check-ignore`` read another repository's
#: index, so a file committed in the inspected worktree could be reported
#: untracked. ``GIT_DIR`` and ``GIT_WORK_TREE`` stay, because a markerless
#: worktree is discovered through them.
_INHERITED_INDEX_VARIABLE = ("GIT_INDEX_FILE",)


@dataclasses.dataclass(slots=True, frozen=True)
class GitLocationAdapter:
    """Answer the location rules from one environment snapshot and runner.

    Every index and ignore query is pinned to the identity the location rules
    validated, through two environment overrides the query supplies: ``GIT_DIR``
    names the absolute git directory that identity resolved, and
    ``GIT_WORK_TREE`` names its worktree top. A relative ``GIT_DIR`` inherited
    from the caller therefore cannot resolve against a different working
    directory and select another repository, and a nested repository cannot
    answer for the file being inspected.

    Attributes
    ----------
    environ
        Environment the commands and the explicit-repository question are
        decided from. The composition root supplies the invocation's snapshot,
        so no query reads the live process environment.
    run_git
        Runs one Git command. It receives the command, the working directory,
        the names to remove from ``environ``, and ``environ`` itself.

    """

    environ: cabc.Mapping[str, str]
    run_git: GitRunner = run

    def names_explicit_repository(self) -> bool:
        """Whether the environment names a repository without a marker."""
        return explicit_repository_names(self.environ, _EXPLICIT_REPOSITORY_VARIABLES)

    def configured_identity(self, start: Path) -> RepositoryIdentity | None:
        """Return the repository the environment names, or ``None``."""
        return self._identity(start, unset=_INHERITED_INDEX_VARIABLE)

    def discovered_identity(self, start: Path) -> RepositoryIdentity | None:
        """Return the repository the checkout itself belongs to, or ``None``."""
        return self._identity(
            start, unset=(*_INHERITED_INDEX_VARIABLE, *_EXPLICIT_REPOSITORY_VARIABLES)
        )

    def tracked_file(self, identity: RepositoryIdentity, relative: Path) -> bool | None:
        """Answer whether ``relative`` is in the index of ``identity``.

        ``ls-files`` parses its pathname arguments as pathspecs, so a directory
        literally named ``state*`` would glob-match unrelated entries. The
        ``literal`` magic makes the query name exactly one file.
        """
        literal_pathspec = f":(literal){relative.as_posix()}"
        listed = self._run_pinned(
            ["git", "ls-files", "--error-unmatch", "--", literal_pathspec],
            identity,
        )
        if listed.returncode == 0:
            return True
        if listed.returncode != 1:
            return None
        # ``--error-unmatch`` exits 1 when the path is absent, but it also
        # exits 1 when the process never ran. Treating that as an index miss
        # would report a file Git never saw as untracked, so a launch failure
        # is an unknown answer that every caller preserves.
        if isinstance(listed, LaunchedProcess) and not listed.launched:
            return None
        return False

    def ignored_entry(
        self, identity: RepositoryIdentity, relative: Path
    ) -> bool | None:
        """Answer whether an ignore rule matches ``relative`` in ``identity``.

        ``./`` stops a leading ``:`` from being parsed as pathspec magic, so
        the check tests the literal entry. A state home under a directory named
        ``:`` would otherwise hand Git a ``:/...`` prefix, which it reads as
        top-relative magic and matches against the wrong path.
        """
        checked = self._run_pinned(
            ["git", "check-ignore", "--quiet", "--", f"./{relative.as_posix()}"],
            identity,
        )
        if checked.returncode == 0:
            return True
        if checked.returncode != 1:
            # Every other non-zero status is a Git failure, not an answer.
            return None
        # ``--quiet`` exits 1 only when no ignore rule matches, but it also
        # exits 1 when the process never ran. Reading that as "no rule
        # matches" would report a path Git never saw as unignored.
        if isinstance(checked, LaunchedProcess) and not checked.launched:
            return None
        return False

    def _run(
        self,
        cmd: list[str],
        cwd: Path,
        unset: tuple[str, ...],
        *,
        environ: cabc.Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one Git command against the supplied environment."""
        return self.run_git(
            cmd,
            cwd,
            unset=unset,
            environ=self.environ if environ is None else environ,
        )

    def _pinned_environment(self, identity: RepositoryIdentity) -> dict[str, str]:
        """Return the environment that pins a query to ``identity``.

        ``GIT_DIR`` and ``GIT_WORK_TREE`` are set to the absolute paths the
        identity resolved, and ``GIT_INDEX_FILE`` is removed so the worktree's
        own index answers. The validated identity is the one authority here:
        an inherited relative value is replaced rather than re-resolved from
        another working directory.
        """
        environ = dict(self.environ)
        for name in _INHERITED_INDEX_VARIABLE:
            environ.pop(name, None)
        environ["GIT_DIR"] = str(identity.git_dir)
        environ["GIT_WORK_TREE"] = str(identity.top)
        return environ

    def _run_pinned(
        self, cmd: list[str], identity: RepositoryIdentity
    ) -> subprocess.CompletedProcess[str]:
        """Run one index or ignore query pinned to ``identity``.

        The command runs from the worktree top, so a relative path Git is given
        resolves the way the marker walk did, while ``GIT_DIR`` keeps the query
        on the repository whose identity was validated.
        """
        return self._run(
            cmd,
            identity.top,
            (),
            environ=self._pinned_environment(identity),
        )

    def _rev_parse_path(
        self, start: Path, option: str, *, unset: tuple[str, ...]
    ) -> Path | None:
        """Return one ``git rev-parse`` path answer, or ``None`` when it cannot."""
        resolved = self._run(["git", "rev-parse", option], start, unset)
        if resolved.returncode != 0:
            return None
        text = resolved.stdout.strip()
        if not text:
            return None
        try:
            return Path(text).resolve()
        except OSError:
            return None

    def _identity(
        self, start: Path, *, unset: tuple[str, ...]
    ) -> RepositoryIdentity | None:
        """Return both halves of a repository identity, or ``None``."""
        git_dir = self._rev_parse_path(start, "--absolute-git-dir", unset=unset)
        top = self._rev_parse_path(start, "--show-toplevel", unset=unset)
        if git_dir is None or top is None:
            return None
        return RepositoryIdentity(git_dir=git_dir, top=top)
