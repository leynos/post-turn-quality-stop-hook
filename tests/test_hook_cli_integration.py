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

#: A free-space floor no real volume meets, which makes the probe short-circuit
#: on the temporary directory without filling a disk.
_EXCEEDS_ANY_VOLUME_MB = 1024**3


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


def _run_hook(repo: Path, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    """Run the hook CLI, feeding it the working directory on stdin."""
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "post_turn_quality_stop_hook.hook"],
        input=json.dumps({"cwd": str(repo)}),
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _write_makefile(repo: Path, *, fails: bool) -> None:
    """Declare one lint target that leaves a relative sentinel when it runs."""
    recipe = "@touch lint-ran; exit 1" if fails else "@touch lint-ran"
    (repo / "Makefile").write_text(
        f"lint:\n\t{recipe}\n",
        encoding="utf-8",
    )


def _write_enospc_makefile(repo: Path) -> None:
    """Declare one lint target that fails as a full disk would."""
    (repo / "Makefile").write_text(
        'lint:\n\t@touch lint-ran; printf "%s\\n" "No space left on device" >&2; '
        "exit 1\n",
        encoding="utf-8",
    )


def _sentinel(repo: Path) -> Path:
    """Return the sentinel the lint recipe creates inside the repository."""
    return repo / "lint-ran"


@pytest.mark.usefixtures("git_env")
def test_disabled_quality_gate_skips_build_targets(tmp_path: Path) -> None:
    """`gate_quality_checks = false` stays silent and runs no build command."""
    repo = _init_repo(tmp_path)
    _write_makefile(repo, fails=True)
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
    assert not _sentinel(repo).exists(), "the disabled gate must not run build targets"


@pytest.mark.usefixtures("git_env")
def test_enabled_quality_gate_blocks_on_a_failing_target(tmp_path: Path) -> None:
    """The default configuration runs the target and reports its failure."""
    repo = _init_repo(tmp_path)
    _write_makefile(repo, fails=True)
    (repo / ".post-turn-quality.toml").write_text(
        _DISABLED_GATES + "gate_quality_checks = true\n",
        encoding="utf-8",
    )

    result = _run_hook(repo)

    assert result.returncode == 0, (
        f"expected exit 0 but got {result.returncode}: {result.stderr!r}"
    )
    assert _sentinel(repo).exists(), "the enabled gate must run the declared target"
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


@pytest.mark.usefixtures("git_env")
def test_disk_exhaustion_in_a_target_allows_the_stop(tmp_path: Path) -> None:
    """A target that fails for lack of space does not block the stop."""
    repo = _init_repo(tmp_path)
    _write_enospc_makefile(repo)
    (repo / ".post-turn-quality.toml").write_text(
        _DISABLED_GATES + "gate_quality_checks = true\n",
        encoding="utf-8",
    )

    # Disable the free-space pre-check so this test proves the failure
    # classification rather than the host's current free space.
    result = _run_hook(repo, POST_TURN_MIN_FREE_MB="0")

    assert result.returncode == 0, (
        f"expected exit 0 but got {result.returncode}: {result.stderr!r}"
    )
    assert _sentinel(repo).exists(), "the enabled gate must run the declared target"
    assert result.stdout == "", (
        f"expected no blocking payload but got {result.stdout!r}"
    )
    assert "quality gates skipped" in result.stderr, (
        f"expected the skip notice on stderr but got {result.stderr!r}"
    )


@pytest.mark.usefixtures("git_env")
def test_low_free_space_skips_the_targets(tmp_path: Path) -> None:
    """A volume below the configured floor skips the gates before they run."""
    repo = _init_repo(tmp_path)
    _write_makefile(repo, fails=True)
    (repo / ".post-turn-quality.toml").write_text(
        _DISABLED_GATES + "gate_quality_checks = true\n",
        encoding="utf-8",
    )

    result = _run_hook(
        repo,
        POST_TURN_MIN_FREE_MB=str(_EXCEEDS_ANY_VOLUME_MB),
    )

    assert result.returncode == 0, (
        f"expected exit 0 but got {result.returncode}: {result.stderr!r}"
    )
    assert not _sentinel(repo).exists(), "a full disk must skip the targets"
    assert result.stdout == "", (
        f"expected no blocking payload but got {result.stdout!r}"
    )
    assert "quality gates skipped" in result.stderr, (
        f"expected the skip notice on stderr but got {result.stderr!r}"
    )
