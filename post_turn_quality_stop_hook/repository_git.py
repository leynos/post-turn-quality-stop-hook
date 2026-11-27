"""Git adapter for the repository-operations port.

The pipeline and the gate module import only
:mod:`.repository_port`. This module is the one implementation that reaches
:mod:`.git`, and the composition edge builds it for the resolved repository
root. Binding the root at construction is what removes the repository path from
the questions gate policy asks, and with it any place a storage location could
enter the decision path.

Every method delegates unchanged, so the port's answer shape is the Git
helper's own and no decision is made here.
"""

from __future__ import annotations

import dataclasses
import typing as typ

from post_turn_quality_stop_hook import git

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.errors import HookError


@dataclasses.dataclass(slots=True, frozen=True)
class GitRepositoryOperations:
    """Answer the repository-operations port about one bound worktree.

    Attributes
    ----------
    repo
        Repository root every question is asked against.

    """

    repo: Path

    def changed_paths_since(
        self, base_commit: str
    ) -> tuple[tuple[str, ...] | None, HookError | None]:
        """Return the sorted, deduplicated paths changed since ``base_commit``."""
        paths, error = git.changed_files(self.repo, base_commit)
        if paths is None:
            return None, error
        return tuple(paths), error

    def has_uncommitted_work(self) -> tuple[bool | None, HookError | None]:
        """Return whether the worktree holds uncommitted or untracked work."""
        return git.has_uncommitted_changes(self.repo)

    def current_branch(self) -> tuple[str | None, HookError | None]:
        """Return the checked-out branch, or ``None`` for a detached ``HEAD``."""
        return git.current_branch(self.repo)
