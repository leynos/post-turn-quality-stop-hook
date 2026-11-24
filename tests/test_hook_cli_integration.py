"""Exercise the hook CLI end to end in temporary Git repositories.

These tests run ``python -m post_turn_quality_stop_hook.hook`` as a child
process, so they cover the command-line behaviour that unit tests exercise
through mocks: configuration loading, environment parsing, and the quality
gate's decision to run or skip build targets.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    from pathlib import Path

GIT = shutil.which("git") or "/usr/bin/git"

_DISABLED_GATES = (
    "gate_uncommitted_changes = false\n"
    "gate_unpushed_commits = false\n"
    "gate_pr_rebase = false\n"
)


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate Git and hook settings from the developer's environment."""
    for key in list(os.environ):
        if key.startswith(("GIT_", "POST_TURN_")) or key in {
            "GH_TOKEN",
            "GITHUB_TOKEN",
        }:
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    # The hook defaults to origin/main; these repositories have no remote, so
    # compare the working tree with the committed HEAD instead.
    monkeypatch.setenv("POST_TURN_BASE_REF", "HEAD")


def _git(repo: Path, *args: str) -> str:
    """Run Git in ``repo`` and return stripped standard output."""
    # The argv list is passed without a shell, and every argument comes from
    # test code, so this helper cannot interpolate untrusted input.
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [GIT, *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=os.environ.copy(),
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    """Create a repository with one commit and an uncommitted code change."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("start\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "start")
    source = repo / "src"
    source.mkdir()
    (source / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return repo


def _run_hook(repo: Path) -> subprocess.CompletedProcess[str]:
    """Run the hook CLI, feeding it the working directory on stdin."""
    return subprocess.run(
        [sys.executable, "-m", "post_turn_quality_stop_hook.hook"],
        input=json.dumps({"cwd": str(repo)}),
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )


def _write_makefile(repo: Path, sentinel: Path, *, fails: bool) -> None:
    """Declare one lint target that leaves a sentinel when it runs."""
    recipe = f"@touch {sentinel}; exit 1" if fails else f"@touch {sentinel}"
    (repo / "Makefile").write_text(
        f"lint:\n\t{recipe}\n",
        encoding="utf-8",
    )


@pytest.mark.usefixtures("git_env")
def test_disabled_quality_gate_skips_build_targets(tmp_path: Path) -> None:
    """`gate_quality_checks = false` stays silent and runs no build command."""
    repo = _init_repo(tmp_path)
    sentinel = tmp_path / "lint-ran"
    _write_makefile(repo, sentinel, fails=True)
    (repo / ".post-turn-quality.toml").write_text(
        _DISABLED_GATES + "gate_quality_checks = false\n",
        encoding="utf-8",
    )

    result = _run_hook(repo)

    assert result.returncode == 0, (
        f"expected exit 0 but got {result.returncode}: {result.stderr!r}"
    )
    assert result.stdout == "", (
        f"expected no output with the quality gate disabled, got {result.stdout!r}"
    )
    assert not sentinel.exists(), "the disabled gate must not run build targets"


@pytest.mark.usefixtures("git_env")
def test_enabled_quality_gate_blocks_on_a_failing_target(tmp_path: Path) -> None:
    """The default configuration runs the target and reports its failure."""
    repo = _init_repo(tmp_path)
    sentinel = tmp_path / "lint-ran"
    _write_makefile(repo, sentinel, fails=True)
    (repo / ".post-turn-quality.toml").write_text(
        _DISABLED_GATES + "gate_quality_checks = true\n",
        encoding="utf-8",
    )

    result = _run_hook(repo)

    assert result.returncode == 0, (
        f"expected exit 0 but got {result.returncode}: {result.stderr!r}"
    )
    assert sentinel.exists(), "the enabled gate must run the declared target"
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block", (
        f"expected a blocking decision but got {payload['decision']!r}"
    )
    assert "Command failed" in payload["reason"], (
        f"expected a command failure in the reason but got {payload['reason']!r}"
    )
    assert "make --no-print-directory lint" in payload["reason"], (
        f"expected the lint command in the reason but got {payload['reason']!r}"
    )
