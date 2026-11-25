"""Shared fixtures for Git plumbing tests.

The repository builders live here rather than in one test module because both
`test_git` and `test_git_location` exercise real Git queries. Keeping them in a
support module means no test module imports another test module.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import typing as typ
from pathlib import Path

import pytest


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess with the given return code and output."""
    return subprocess.CompletedProcess(
        args=["unit-test"], returncode=returncode, stdout=stdout, stderr=stderr
    )


REPO = Path("/fake/repo")


def _run_git(repo: Path, *args: str) -> None:
    """Run one Git command in ``repo`` with a fixed test identity."""
    git_program = shutil.which("git")
    if git_program is None:
        pytest.skip("git is required for repository fixtures")
    subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # fixed argv, no shell.
        [git_program, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


def _real_repository(path: Path) -> Path:
    """Create a Git repository with one committed file."""
    repo = path / "repo"
    repo.mkdir()
    env = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    git_program = shutil.which("git")
    if git_program is None:
        pytest.skip("git is required for repository fixtures")

    def git(*args: str) -> None:
        """Run one Git command in the new repository."""
        subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # fixed argv, no shell.
            [git_program, *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

    git("init", "-q", "-b", "main")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "initial")
    return repo


def _run_git_with_environment(cwd: Path, *args: str) -> None:
    """Run one Git command in ``cwd``, inheriting the process environment.

    A worktree configured through ``GIT_DIR`` and ``GIT_WORK_TREE`` needs those
    variables to reach Git, which the fixed environment in ``_run_git`` would
    drop. The test identity is still supplied explicitly so the command cannot
    depend on the developer's own Git configuration.
    """
    git_program = shutil.which("git")
    if git_program is None:
        pytest.skip("git is required for repository fixtures")
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    })
    subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # fixed argv, no shell.
        [git_program, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def resolver_failing_for(alias: Path) -> typ.Callable[..., Path]:
    """Return a resolver that raises for ``alias`` and defers otherwise."""
    original = Path.resolve

    def _resolve(path: Path, *, strict: bool = False) -> Path:
        """Resolve every path except the failing alias."""
        if path == alias:
            raise OSError
        return original(path, strict=strict)

    return _resolve
