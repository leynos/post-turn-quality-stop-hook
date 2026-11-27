"""Exercise the unpushed gate against real Git remotes.

These tests reproduce the two failure modes from issue #193:

- a stale ``refs/remotes/origin/<branch>`` after an explicit-URL push must
  not block when the remote already has the commit;
- a URL-valued upstream must not silently skip when commits are unpushed.

They also pin the verification fetch's side effects: it must read the remote's
actual head without moving the local remote-tracking ref, and it must not let
shared state such as ``FETCH_HEAD`` decide what the gate compares.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import typing as typ

import pytest

from post_turn_quality_stop_hook import branch_gates as branch_gates_mod
from post_turn_quality_stop_hook import formatting as formatting_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook import upstream as upstream_mod
from post_turn_quality_stop_hook.branch_state_git import GitBranchState
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.git import get_upstream_ref
from post_turn_quality_stop_hook.git_facts import collect_git_facts

if typ.TYPE_CHECKING:
    from pathlib import Path

# ``subprocess`` runs Git and the hook CLI as real child processes. Every call
# passes an argv list with no shell, and every argument comes from test code,
# so no untrusted value reaches a command line.
import subprocess  # ruff:ignore[suspicious-subprocess-import]

# Resolve Git through PATH like the production commands do, while keeping the
# executable out of the literal argv that the partial-path lint rule inspects.
GIT = shutil.which("git") or "/usr/bin/git"


def _no_warnings(_message: str) -> None:
    """Discard one operator warning the test does not inspect."""


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate Git and hook settings from the developer's environment."""
    # Inherited GIT_DIR or GIT_INDEX_FILE would redirect the scratch
    # repositories at the developer's own checkout, so clear every GIT_*
    # variable before setting the ones these tests need. POST_TURN_* settings
    # can select a base ref or behaviour the scratch hub does not provide, so
    # they are cleared too.
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


def _git(repo: Path, *args: str) -> str:
    """Run Git in ``repo`` and return stripped stdout.

    The argv list is passed without a shell, and every argument comes from
    test code, so this helper cannot interpolate untrusted input into a
    command line.
    """
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [GIT, *args],
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
    """Commit a changed file and return the new head commit."""
    (work / "file.txt").write_text(content, encoding="utf-8")
    _git(work, "commit", "-am", message)
    return _git(work, "rev-parse", "HEAD")


def _advance_feature_from_a_clone(tmp_path: Path, hub: Path) -> str:
    """Advance the hub's feature branch from a separate clone.

    The working clone stays behind, so a test can distinguish the hub's head
    from the commit that clone already has.
    """
    clone = tmp_path / "other"
    _git(tmp_path, "clone", "--quiet", str(hub), str(clone))
    _git(clone, "checkout", "feature")
    (clone / "file.txt").write_text("three\n", encoding="utf-8")
    _git(clone, "commit", "-am", "three")
    head = _git(clone, "rev-parse", "HEAD")
    _git(clone, "push", "origin", "feature")
    return head


def _refresh(
    work: Path, config: Config | None = None
) -> branch_gates_mod.BranchStateGateDecision | upstream_mod.UpstreamComparison:
    """Collect the facts and refresh the upstream for ``work``."""
    settings = config or Config()
    facts = collect_git_facts(work, settings)
    options = state_mod.StopCheckOptions(
        always_fetch=False,
        max_out=12000,
        config=settings,
    )
    source = upstream_mod.GitUpstreamHeadSource(
        work,
        facts,
        refresh_upstream=settings.unpushed_commits_refresh_upstream,
        fetch_timeout_seconds=settings.unpushed_commits_fetch_timeout_seconds,
        on_warning=_no_warnings,
    )
    return branch_gates_mod.refresh_upstream_head(
        work, facts, options, source, branch_state=GitBranchState()
    )


def _gate_decision(
    work: Path, config: Config | None = None
) -> branch_gates_mod.BranchStateGateDecision:
    """Run the unpushed gate against facts collected for ``work``."""
    return branch_gates_mod.unpushed_commits_gate(_refresh(work, config))


def _run_hook(work: Path) -> subprocess.CompletedProcess[str]:
    """Run the hook CLI as a child process, feeding it the working directory."""
    return subprocess.run(
        [sys.executable, "-m", "post_turn_quality_stop_hook.hook"],
        input=json.dumps({"cwd": str(work)}),
        cwd=work,
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )


@pytest.mark.usefixtures("git_env")
def test_explicit_url_push_with_stale_tracking_ref_passes(tmp_path: Path) -> None:
    """A remote that already has HEAD passes despite a stale local ref."""
    hub, work = _init_repos(tmp_path)
    stale_ref = _git(work, "rev-parse", "origin/feature")
    pushed_head = _commit_file(work, "three\n", "three")
    _git(work, "push", str(hub), "HEAD:refs/heads/feature")
    # Git 2.56 updates a remote-tracking ref when the pushed URL uniquely
    # matches a configured remote. Reset it explicitly so the stale-cache
    # premise this test exercises holds on every Git version.
    _git(work, "update-ref", "refs/remotes/origin/feature", stale_ref)

    assert _git(work, "rev-parse", "origin/feature") == stale_ref, (
        "the fixture must leave the tracking ref behind the remote head"
    )
    assert _git(work, "ls-remote", str(hub), "refs/heads/feature").startswith(
        pushed_head
    ), "the push must have reached the bare hub"

    decision = _gate_decision(work)

    assert decision.error is None, (
        f"the refreshed upstream must be read successfully, got {decision.error!r}"
    )
    assert decision.should_block is False, (
        "a remote that already has HEAD must not trigger the unpushed block"
    )
    assert decision.outcome == "pass", (
        "the gate must report a verified pass, not a skip"
    )


@pytest.mark.usefixtures("git_env")
def test_url_upstream_with_unpushed_commit_blocks(tmp_path: Path) -> None:
    """A URL-valued upstream still blocks when HEAD is genuinely ahead."""
    hub, work = _init_repos(tmp_path)
    _git(work, "push", "-u", str(hub), "HEAD:refs/heads/feature")
    _commit_file(work, "three\n", "three")

    upstream_ref, upstream_err = get_upstream_ref(work)
    assert upstream_ref is None, "a URL-valued upstream must not resolve through @{u}"
    assert upstream_err is not None, (
        "a URL-valued upstream is expected to defeat @{u} resolution"
    )

    facts = collect_git_facts(work, Config())
    assert facts.upstream_remote == str(hub), "the URL must be read as the remote"
    assert facts.upstream_branch == "feature", "the branch name must be resolved"
    assert facts.upstream_error is None, (
        f"the upstream must be readable, got {facts.upstream_error!r}"
    )

    refresh = _refresh(work)
    assert isinstance(refresh, upstream_mod.UpstreamComparison), (
        "an ahead branch must produce a comparison, not a terminal decision"
    )
    decision = branch_gates_mod.unpushed_commits_gate(refresh)

    assert decision.should_block is True, (
        "a URL-valued upstream must still block when HEAD is ahead"
    )
    assert decision.outcome == "block", (
        "a URL-valued upstream with an unpushed commit must report a block"
    )
    label = formatting_mod.upstream_label(refresh)
    assert "feature on" in label, (
        f"a URL upstream must be labelled by branch and remote, got {label!r}"
    )


@pytest.mark.usefixtures("git_env")
def test_fetch_prefers_the_branch_over_a_same_named_tag(tmp_path: Path) -> None:
    """A tag sharing the branch name must not shadow the upstream branch."""
    hub, work = _init_repos(tmp_path)
    stale_ref = _git(work, "rev-parse", "origin/feature")
    main_head = _git(work, "rev-parse", "main")
    _git(work, "tag", "feature", main_head)
    _git(work, "push", str(hub), "refs/tags/feature")
    pushed_head = _commit_file(work, "three\n", "three")
    _git(work, "push", str(hub), "HEAD:refs/heads/feature")
    # Keep the tracking ref stale so the test cannot pass by falling back to
    # the local cache instead of fetching the branch or the tag.
    _git(work, "update-ref", "refs/remotes/origin/feature", stale_ref)

    decision = _gate_decision(work)

    assert decision.error is None, (
        f"the branch fetch must succeed despite the tag, got {decision.error!r}"
    )
    assert decision.should_block is False, (
        "the fetched branch, not the same-named tag, must decide the gate"
    )
    assert decision.outcome == "pass", (
        "the branch fetch must report a verified pass, not a skip"
    )
    assert _git(work, "ls-remote", str(hub), "refs/heads/feature").startswith(
        pushed_head
    ), "the hub must still hold the pushed feature head"


@pytest.mark.usefixtures("git_env")
def test_named_remote_fetch_reads_the_remote_head_without_moving_the_tracking_ref(
    tmp_path: Path,
) -> None:
    """The verification fetch must not reset a later force-with-lease lease."""
    hub, work = _init_repos(tmp_path)
    stale_ref = _git(work, "rev-parse", "origin/feature")
    remote_head = _advance_feature_from_a_clone(tmp_path, hub)

    # Give the working clone the new head without leaving its remote-tracking
    # ref updated: the gate must read the hub itself, not this cache.
    _git(work, "fetch", "--quiet", "origin", "feature")
    fetched = _git(work, "rev-parse", "FETCH_HEAD")
    _git(work, "reset", "--hard", fetched)
    _git(work, "update-ref", "refs/remotes/origin/feature", stale_ref)
    assert fetched == remote_head, "the fixture must fetch the advanced head"

    decision = _gate_decision(work)

    assert decision.error is None, (
        f"the named-remote fetch must succeed, got {decision.error!r}"
    )
    assert decision.should_block is False, (
        "the gate must compare against the fetched head, not the stale cache"
    )
    assert _git(work, "rev-parse", "origin/feature") == stale_ref, (
        "the verification fetch must leave the remote-tracking ref unchanged"
    )


@pytest.mark.usefixtures("git_env")
def test_fetch_result_survives_a_competing_fetch(tmp_path: Path) -> None:
    """A fetch of another branch between fetch and read cannot replace it."""
    hub, work = _init_repos(tmp_path)
    local_feature = _git(work, "rev-parse", "feature")
    main_head = _git(work, "rev-parse", "main")
    remote_head = _advance_feature_from_a_clone(tmp_path, hub)
    assert remote_head != local_feature, (
        "the fixture must leave the local branch behind the hub"
    )

    original_run = upstream_mod.run
    competing_fetches: list[bool] = []

    def run_with_competing_fetch(
        cmd: list[str], cwd: Path, *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the command, then replace FETCH_HEAD once after a fetch."""
        result = original_run(cmd, cwd, timeout=timeout)
        if cmd[:2] == ["git", "fetch"] and not competing_fetches:
            competing_fetches.append(True)
            # Use the checked helper so a failed competing fetch fails the
            # test, and prove it replaced the shared FETCH_HEAD.
            _git(cwd, "fetch", "--quiet", "origin", "main")
            assert _git(cwd, "rev-parse", "FETCH_HEAD") == main_head, (
                "the competing fetch must write its own result to FETCH_HEAD"
            )
        return result

    head, err = upstream_mod.fetch_upstream_head(
        work,
        "origin",
        "refs/heads/feature",
        options=upstream_mod.FetchOptions(
            on_warning=_no_warnings, runner=run_with_competing_fetch
        ),
    )

    assert err is None, f"the fetch must succeed, got {err!r}"
    assert competing_fetches == [True], "the competing fetch must have run"
    assert head == remote_head, (
        "the private fetch ref must keep its result across the competing fetch"
    )


@pytest.mark.usefixtures("git_env")
def test_hook_blocks_before_the_push_and_passes_after(tmp_path: Path) -> None:
    """The full hook distinguishes a real pass from a skipped or failed gate."""
    hub, work = _init_repos(tmp_path)
    stale_ref = _git(work, "rev-parse", "origin/feature")
    pushed_head = _commit_file(work, "three\n", "three")

    before = _run_hook(work)

    assert before.returncode == 0, (
        f"the hook must exit 0, got {before.returncode!r}: {before.stderr}"
    )
    before_payload = json.loads(before.stdout)
    assert before_payload["decision"] == "block", (
        f"an unpushed commit must block, got {before_payload!r}"
    )
    assert "push" in before_payload["reason"].lower(), (
        f"the block must be the push reminder, got {before_payload['reason']!r}"
    )

    _git(work, "push", str(hub), "HEAD:refs/heads/feature")
    _git(work, "update-ref", "refs/remotes/origin/feature", stale_ref)

    after = _run_hook(work)

    assert after.returncode == 0, (
        f"the hook must exit 0, got {after.returncode!r}: {after.stderr}"
    )
    assert after.stdout == "", (
        "a verified pass must emit no decision payload; empty output alone is "
        "not enough, so the pushes above prove the gate actually ran"
    )
    assert "could not verify the upstream" not in after.stderr, (
        "a pass must not come from a failed upstream refresh"
    )
    assert _git(work, "ls-remote", str(hub), "refs/heads/feature").startswith(
        pushed_head
    ), "the push must have reached the bare hub"
