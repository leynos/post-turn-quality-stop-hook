"""Exercise the subprocess environment and run() error fallbacks."""

from __future__ import annotations

import os
import typing as typ
from pathlib import Path
from unittest import mock

import pytest
from quality_support import UnusedRepositoryFacts

from post_turn_quality_stop_hook import pipeline as pipeline_mod
from post_turn_quality_stop_hook import process as process_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook import upstream as upstream_mod

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook.git_facts import GitFacts


def _unused_upstream_source(
    _repo: Path,
    _facts: GitFacts | None,
    _observer: object,
) -> upstream_mod.GitUpstreamHeadSource:
    """Build a port that a preparation failure must never consult."""
    return upstream_mod.GitUpstreamHeadSource(
        _repo,
        _facts,
        refresh_upstream=True,
        fetch_timeout_seconds=10.0,
    )


class TestRunOSError:
    """Tests for run() handling of OSError (e.g. missing cwd)."""

    def test_nonexistent_cwd_returns_error(self) -> None:
        """run() with a nonexistent cwd returns rc=1 instead of raising."""
        missing_path = Path("/nonexistent/path")
        result = process_mod.run(["git", "status"], missing_path)
        assert result.returncode == 1, (
            f"expected returncode 1 for missing cwd but got {result.returncode!r}"
        )
        assert result.args == ["git", "status"], (
            f"expected args preserved but got {result.args!r}"
        )
        assert result.stdout == "", f"expected empty stdout but got {result.stdout!r}"
        assert result.stderr, "expected stderr to describe missing cwd"
        assert str(missing_path) in result.stderr, (
            f"expected missing path in stderr but got {result.stderr!r}"
        )

    def test_run_stop_checks_nonexistent_cwd(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Full pipeline exits cleanly when start_cwd does not exist."""
        with mock.patch("shutil.which", return_value="/usr/bin/git"):
            rc = pipeline_mod.run_stop_checks(
                Path("/nonexistent/path"),
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
                _unused_upstream_source,
                repository_facts_source=UnusedRepositoryFacts(),
                warn=_no_warnings,
            )
        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        assert capsys.readouterr().out == "", (
            "expected no hook output when start_cwd does not exist"
        )


# ---------------------------------------------------------------------------
# _subprocess_env
# ---------------------------------------------------------------------------


_EXTRA_DIR_PARTS = (
    (".local", "bin"),
    (".bun", "bin"),
    (".cargo", "bin"),
    (".lody", "bin"),
    ("go", "bin"),
)


class TestSubprocessEnv:
    """Tests for _subprocess_env() PATH enrichment.

    Kills the ``_subprocess_env`` PATH-enrichment survivors tracked in #30.
    """

    def test_prepends_each_tool_dir_once_in_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existing tool dirs are prepended, longest-standing last, once each."""
        for parts in _EXTRA_DIR_PARTS:
            tmp_path.joinpath(*parts).mkdir(parents=True)
        monkeypatch.setattr(
            process_mod.Path, "home", classmethod(lambda _cls: tmp_path)
        )
        monkeypatch.setenv("PATH", "/usr/bin")

        entries = process_mod._subprocess_env()["PATH"].split(os.pathsep)

        # extra_dirs are inserted at index 0 in list order, so the final
        # ordering reverses them and places the pre-existing PATH last.
        expected = [
            str(tmp_path.joinpath("go", "bin")),
            str(tmp_path.joinpath(".lody", "bin")),
            str(tmp_path.joinpath(".cargo", "bin")),
            str(tmp_path.joinpath(".bun", "bin")),
            str(tmp_path.joinpath(".local", "bin")),
            "/usr/bin",
        ]
        assert entries == expected, f"expected {expected!r} but got {entries!r}"

    def test_handles_unset_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unset PATH defaults to empty, so only tool dirs are added."""
        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True)
        monkeypatch.setattr(
            process_mod.Path, "home", classmethod(lambda _cls: tmp_path)
        )
        monkeypatch.delenv("PATH", raising=False)

        entries = process_mod._subprocess_env()["PATH"].split(os.pathsep)

        # env.get("PATH", "") must default to "", whose split yields [""].
        assert entries == [str(local_bin), ""], (
            f"expected only the tool dir and the empty tail but got {entries!r}"
        )

    def test_removes_colour_forcing_variables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Captured child output must not inherit colour-forcing settings."""
        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.setenv("CLICOLOR_FORCE", "1")
        monkeypatch.delenv("NO_COLOR", raising=False)

        env = process_mod._subprocess_env()

        assert "FORCE_COLOR" not in env, "expected FORCE_COLOR to be removed"
        assert "CLICOLOR_FORCE" not in env, "expected CLICOLOR_FORCE to be removed"
        assert env["NO_COLOR"] == "1", (
            f"expected NO_COLOR '1' but got {env['NO_COLOR']!r}"
        )

    def test_skips_missing_dirs_and_does_not_duplicate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only existing dirs are added, and present entries are not repeated."""
        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True)  # only this tool dir exists
        monkeypatch.setattr(
            process_mod.Path, "home", classmethod(lambda _cls: tmp_path)
        )
        monkeypatch.setenv("PATH", os.pathsep.join([str(local_bin), "/usr/bin"]))

        entries = process_mod._subprocess_env()["PATH"].split(os.pathsep)

        # The absent dirs (guard requires is_dir()) must not appear, and the
        # already-present .local/bin must not be duplicated.
        assert entries == [str(local_bin), "/usr/bin"], (
            f"expected no additions or duplicates but got {entries!r}"
        )

    def test_unset_names_are_removed_for_one_command_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A named variable is dropped from the copy, not from the process."""
        monkeypatch.setenv("GIT_INDEX_FILE", "/elsewhere/index")

        env = process_mod._subprocess_env(("GIT_INDEX_FILE",))

        assert "GIT_INDEX_FILE" not in env, (
            "an inherited index file must be removed from the child environment"
        )
        assert os.environ["GIT_INDEX_FILE"] == "/elsewhere/index", (
            "the process environment must be left untouched for other commands"
        )
        assert "GIT_INDEX_FILE" in process_mod._subprocess_env(), (
            "a command that does not name the variable keeps the inherited value"
        )

    def test_run_passes_the_removed_names_to_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``run(unset=...)`` removes a variable the child would inherit.

        The inherited value is seeded first. Without it the test would pass
        even if ``run`` ignored ``unset``, because an absent variable also
        prints the fallback.
        """
        monkeypatch.setenv("GIT_INDEX_FILE", "/elsewhere/index")
        result = process_mod.run(
            ["sh", "-c", 'printf "%s" "${GIT_INDEX_FILE-unset}"'],
            tmp_path,
            unset=("GIT_INDEX_FILE",),
        )

        assert result.returncode == 0, (
            f"the child must launch successfully, got status {result.returncode!r}"
            f" and stderr {result.stderr!r}"
        )
        assert result.stdout == "unset", (
            f"expected the variable to be absent in the child but got {result.stdout!r}"
        )


def _no_warnings(_message: str) -> None:
    """Discard one operator warning."""
