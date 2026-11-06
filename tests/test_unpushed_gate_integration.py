"""Exercise the unpushed gate against real Git remotes.

These tests reproduce the two failure modes from issue #193:

- a stale ``refs/remotes/origin/<branch>`` after an explicit-URL push must
  not block when the remote already has the commit;
- a URL-valued upstream must not silently skip when commits are unpushed.
"""

from __future__ import annotations

import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import typing as typ

import pytest

from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.git import get_upstream_ref
from post_turn_quality_stop_hook.git_facts import collect_git_facts

if typ.TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate Git from host and Lody configuration for scratch repositories."""
    for key in list(os.environ):
        if key.startswith(("GIT_CONFIG_", "GIT_SSH")) or key in {
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


def _git(repo: Path, *args: str) -> str:
    """Run Git in ``repo`` and return stripped stdout."""
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        ["/usr/bin/git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=os.environ.copy(),
    )
    return result.stdout.strip()


def _init_repos(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare hub and a working clone with a pushed feature branch."""
    hub = tmp_path / "hub.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(hub))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    (work / "file.txt").write_text("one\n", encoding="utf-8")
    _git(work, "add", "file.txt")
    _git(work, "commit", "-m", "one")
    _git(work, "remote", "add", "origin", str(hub))
    _git(work, "push", "-u", "origin", "main")
    _git(work, "checkout", "-b", "feature")
    (work / "file.txt").write_text("two\n", encoding="utf-8")
    _git(work, "commit", "-am", "two")
    _git(work, "push", "-u", "origin", "feature")
    return hub, work


def _commit_file(work: Path, content: str, message: str) -> str:
    (work / "file.txt").write_text(content, encoding="utf-8")
    _git(work, "commit", "-am", message)
    return _git(work, "rev-parse", "HEAD")


def _gate_decision(
    work: Path, config: Config | None = None
) -> pipeline_mod.BranchStateGateDecision:
    facts = collect_git_facts(work, config or Config())
    options = state_mod.StopCheckOptions(
        always_fetch=False,
        max_out=12000,
        config=config or Config(),
    )
    return pipeline_mod.unpushed_commits_gate(
        work, state_mod.HookState(git_facts=facts), options
    )


@pytest.mark.usefixtures("git_env")
def test_explicit_url_push_with_stale_tracking_ref_passes(tmp_path: Path) -> None:
    """A remote that already has HEAD passes despite a stale local ref."""
    hub, work = _init_repos(tmp_path)
    stale_ref = _git(work, "rev-parse", "origin/feature")
    pushed_head = _commit_file(work, "three\n", "three")
    _git(work, "push", str(hub), "HEAD:refs/heads/feature")

    assert _git(work, "rev-parse", "origin/feature") == stale_ref
    assert _git(work, "ls-remote", str(hub), "refs/heads/feature").startswith(
        pushed_head
    )

    decision = _gate_decision(work)

    assert decision.should_block is False
    assert decision.outcome == "pass"


@pytest.mark.usefixtures("git_env")
def test_url_upstream_with_unpushed_commit_blocks(tmp_path: Path) -> None:
    """A URL-valued upstream still blocks when HEAD is genuinely ahead."""
    hub, work = _init_repos(tmp_path)
    _git(work, "push", "-u", str(hub), "HEAD:refs/heads/feature")
    _commit_file(work, "three\n", "three")

    upstream_ref, upstream_err = get_upstream_ref(work)
    assert upstream_ref is None
    assert upstream_err is not None, (
        "a URL-valued upstream is expected to defeat @{u} resolution"
    )

    facts = collect_git_facts(work, Config())
    assert facts.upstream_remote == str(hub)
    assert facts.upstream_branch == "feature"
    assert facts.upstream_error is None

    decision = _gate_decision(work)

    assert decision.should_block is True
    assert decision.outcome == "block"
    assert decision.payload is not None
    assert "feature on" in decision.payload["reason"]
