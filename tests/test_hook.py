"""Exercise the post-turn quality stop hook end to end and in pieces.

This module covers the hook's expected git-state decisions, environment
parsing, subprocess error handling, make-target discovery, and compush
follow-up behavior. The tests focus on observable inputs and outputs,
including successful checks, soft-failure paths that must stay silent,
and blocking/error conditions that should surface through the hook
contract.

Run the suite from the repository root with `pytest` or the repository's
test target. No external fixtures or environment setup are required
beyond a Python environment with `pytest` installed because the tests
mock subprocess-heavy interactions.

Example:
    python3 -m pytest tests/test_hook.py -v

"""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404
from pathlib import Path
from unittest import mock

import pytest

from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import execution as exec_mod
from post_turn_quality_stop_hook import git as git_mod
from post_turn_quality_stop_hook import hook
from post_turn_quality_stop_hook import state as state_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["unit-test"], returncode=returncode, stdout=stdout, stderr=stderr
    )


REPO = Path("/fake/repo")


# ---------------------------------------------------------------------------
# has_uncommitted_changes
# ---------------------------------------------------------------------------


class TestHasUncommittedChanges:
    """Tests for has_uncommitted_changes()."""

    def test_clean_working_tree(self) -> None:
        """All three checks pass -> False."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0),  # git diff --quiet
                _completed(0),  # git diff --cached --quiet
                _completed(0, stdout=""),  # git ls-files
            ]
            dirty, err = git_mod.has_uncommitted_changes(REPO)
        assert dirty is False, f"expected dirty to be False but was {dirty!r}"
        assert err is None, f"expected no error but got {err!r}"
        expected_calls = 3
        assert mock_run.call_count == expected_calls
        assert mock_run.call_args_list[0] == mock.call(["git", "diff", "--quiet"], REPO)
        assert mock_run.call_args_list[1] == mock.call(
            ["git", "diff", "--cached", "--quiet"], REPO
        )
        assert mock_run.call_args_list[2] == mock.call(
            ["git", "ls-files", "--others", "--exclude-standard"], REPO
        )

    def test_unstaged_changes(self) -> None:
        """Git diff --quiet exits 1 -> True."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(1)
            dirty, err = git_mod.has_uncommitted_changes(REPO)
        assert dirty is True, f"expected dirty to be True but was {dirty!r}"
        assert err is None, f"expected no error but got {err!r}"
        mock_run.assert_called_once_with(["git", "diff", "--quiet"], REPO)

    def test_staged_changes(self) -> None:
        """Git diff --cached --quiet exits 1 -> True."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0),  # unstaged clean
                _completed(1),  # staged dirty
            ]
            dirty, err = git_mod.has_uncommitted_changes(REPO)
        assert dirty is True, f"expected dirty to be True but was {dirty!r}"
        assert err is None, f"expected no error but got {err!r}"
        expected_calls = 2
        assert mock_run.call_count == expected_calls
        assert mock_run.call_args_list[0] == mock.call(["git", "diff", "--quiet"], REPO)
        assert mock_run.call_args_list[1] == mock.call(
            ["git", "diff", "--cached", "--quiet"], REPO
        )

    def test_untracked_files(self) -> None:
        """ls-files returns output -> True."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0),
                _completed(0),
                _completed(0, stdout="newfile.py\n"),
            ]
            dirty, err = git_mod.has_uncommitted_changes(REPO)
        assert dirty is True, f"expected dirty to be True but was {dirty!r}"
        assert err is None, f"expected no error but got {err!r}"
        assert mock_run.call_args_list[2] == mock.call(
            ["git", "ls-files", "--others", "--exclude-standard"], REPO
        )

    def test_diff_error(self) -> None:
        """Non-0/1 exit from diff -> None + error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(128, stderr="fatal: bad")
            dirty, err = git_mod.has_uncommitted_changes(REPO)
        assert dirty is None, f"expected dirty to be None on error but was {dirty!r}"
        assert err is not None, "expected an error message from git diff failure"
        assert "fatal: bad" in err, f"expected fatal error in message but got {err!r}"
        mock_run.assert_called_once_with(["git", "diff", "--quiet"], REPO)

    def test_ls_files_error(self) -> None:
        """ls-files failure -> None + error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0),
                _completed(0),
                _completed(128, stderr="fatal: oops"),
            ]
            dirty, err = git_mod.has_uncommitted_changes(REPO)
        assert dirty is None, f"expected dirty to be None on error but was {dirty!r}"
        assert err is not None, "expected an error message from git ls-files failure"
        assert "git ls-files failed" in err, (
            f"expected ls-files failure in message but got {err!r}"
        )
        assert mock_run.call_args_list[2] == mock.call(
            ["git", "ls-files", "--others", "--exclude-standard"], REPO
        )


# ---------------------------------------------------------------------------
# get_upstream_ref
# ---------------------------------------------------------------------------


class TestGetUpstreamRef:
    """Tests for get_upstream_ref()."""

    def test_returns_upstream(self) -> None:
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="origin/main\n")
            ref, err = git_mod.get_upstream_ref(REPO)
        assert ref == "origin/main", (
            f"expected upstream ref origin/main but got {ref!r}"
        )
        assert err is None, f"expected no error but got {err!r}"
        mock_run.assert_called_once_with(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            REPO,
        )

    def test_no_upstream(self) -> None:
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(128, stderr="no upstream")
            ref, err = git_mod.get_upstream_ref(REPO)
        assert ref is None, f"expected no upstream ref but got {ref!r}"
        assert "no upstream" in (err or ""), (
            f"expected no-upstream message but got {err!r}"
        )
        mock_run.assert_called_once_with(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            REPO,
        )

    def test_empty_stdout(self) -> None:
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="")
            ref, err = git_mod.get_upstream_ref(REPO)
        assert ref is None, f"expected no upstream ref but got {ref!r}"
        assert err is not None, "expected an error when upstream stdout is empty"
        mock_run.assert_called_once_with(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            REPO,
        )


# ---------------------------------------------------------------------------
# has_unpushed_commits
# ---------------------------------------------------------------------------


class TestHasUnpushedCommits:
    """Tests for has_unpushed_commits()."""

    def test_ahead_of_upstream(self) -> None:
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="2\n")
            ahead, err = git_mod.has_unpushed_commits(REPO, "origin/main")
        assert ahead is True, f"expected ahead to be True but was {ahead!r}"
        assert err is None, f"expected no error but got {err!r}"
        mock_run.assert_called_once_with(
            ["git", "rev-list", "--count", "origin/main..HEAD"], REPO
        )

    def test_not_ahead_of_upstream(self) -> None:
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="0\n")
            ahead, err = git_mod.has_unpushed_commits(REPO, "origin/main")
        assert ahead is False, f"expected ahead to be False but was {ahead!r}"
        assert err is None, f"expected no error but got {err!r}"
        mock_run.assert_called_once_with(
            ["git", "rev-list", "--count", "origin/main..HEAD"], REPO
        )

    def test_rev_list_error(self) -> None:
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(128, stderr="fatal: bad revision")
            ahead, err = git_mod.has_unpushed_commits(REPO, "origin/main")
        assert ahead is None, f"expected ahead to be None on error but was {ahead!r}"
        assert err is not None, "expected an error message from rev-list failure"
        assert "fatal: bad revision" in err, (
            f"expected bad revision error in message but got {err!r}"
        )
        mock_run.assert_called_once_with(
            ["git", "rev-list", "--count", "origin/main..HEAD"], REPO
        )

    def test_empty_output(self) -> None:
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="")
            ahead, err = git_mod.has_unpushed_commits(REPO, "origin/main")
        assert ahead is None, (
            f"expected ahead to be None for empty output but was {ahead!r}"
        )
        assert "empty output" in (err or ""), (
            f"expected empty output error but got {err!r}"
        )
        mock_run.assert_called_once_with(
            ["git", "rev-list", "--count", "origin/main..HEAD"], REPO
        )

    def test_non_integer_output(self) -> None:
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="two\n")
            ahead, err = git_mod.has_unpushed_commits(REPO, "origin/main")
        assert ahead is None, (
            f"expected ahead to be None for non-integer output but was {ahead!r}"
        )
        assert "non-integer" in (err or ""), (
            f"expected non-integer error but got {err!r}"
        )
        mock_run.assert_called_once_with(
            ["git", "rev-list", "--count", "origin/main..HEAD"], REPO
        )


# ---------------------------------------------------------------------------
# compush_check
# ---------------------------------------------------------------------------


class TestCompushCheck:
    """Tests for compush_check()."""

    def test_dirty_with_upstream(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Dirty tree + upstream -> block with push message."""
        with (
            mock.patch.object(
                state_mod, "has_uncommitted_changes", return_value=(True, None)
            ),
            mock.patch.object(
                state_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
        ):
            rc = state_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block", (
            f"expected block decision but got {out['decision']!r}"
        )
        assert "Please commit and push to origin/feature" in out["reason"], (
            f"expected commit/push reminder but got {out['reason']!r}"
        )

    def test_dirty_no_upstream(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Dirty tree + no upstream -> block with fallback text."""
        with (
            mock.patch.object(
                state_mod, "has_uncommitted_changes", return_value=(True, None)
            ),
            mock.patch.object(
                state_mod, "get_upstream_ref", return_value=(None, "no upstream")
            ),
        ):
            rc = state_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block", (
            f"expected block decision but got {out['decision']!r}"
        )
        assert "origin (upstream not configured)" in out["reason"], (
            f"expected upstream fallback in reason but got {out['reason']!r}"
        )

    def test_clean_tree(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Clean tree -> no output, exit 0."""
        with (
            mock.patch.object(
                state_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
            mock.patch.object(
                state_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(
                state_mod, "has_unpushed_commits", return_value=(False, None)
            ),
        ):
            rc = state_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        assert capsys.readouterr().out == "", "expected no hook output for clean tree"

    def test_error_checking_changes(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Error from has_uncommitted_changes -> silent exit 0."""
        with mock.patch.object(
            state_mod, "has_uncommitted_changes", return_value=(None, "oops")
        ):
            rc = state_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        assert capsys.readouterr().out == "", (
            "expected no hook output when change check errors are suppressed"
        )

    def test_clean_tree_with_unpushed_commits(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Clean tree + ahead of upstream -> block with push-only message."""
        with (
            mock.patch.object(
                state_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
            mock.patch.object(
                state_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(
                state_mod, "has_unpushed_commits", return_value=(True, None)
            ),
        ):
            rc = state_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block", (
            f"expected block decision but got {out['decision']!r}"
        )
        assert "Please push committed changes to origin/feature" in out["reason"], (
            f"expected push reminder but got {out['reason']!r}"
        )

    def test_clean_tree_no_upstream(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Clean tree + no upstream -> no ahead check and no output."""
        with (
            mock.patch.object(
                state_mod, "get_upstream_ref", return_value=(None, "no upstream")
            ),
            mock.patch.object(
                state_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(state_mod, "has_unpushed_commits") as mock_ahead,
        ):
            rc = state_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        mock_ahead.assert_not_called()
        assert capsys.readouterr().out == "", (
            "expected no hook output when upstream is unavailable"
        )

    def test_error_checking_unpushed_commits(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ahead check errors stay silent to preserve hook contract."""
        with (
            mock.patch.object(
                state_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
            mock.patch.object(
                state_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(
                state_mod, "has_unpushed_commits", return_value=(None, "oops")
            ),
        ):
            rc = state_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        assert capsys.readouterr().out == "", (
            "expected no hook output when ahead check errors are suppressed"
        )


# ---------------------------------------------------------------------------
# parse_env - compush flag
# ---------------------------------------------------------------------------


class TestParseEnvCompush:
    """Tests for the compush flag in parse_env()."""

    def test_compush_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POST_TURN_COMPUSH", "1")
        _base, options = hook.parse_env()
        assert options.compush is True, (
            f"expected compush to be True but was {options.compush!r}"
        )

    def test_compush_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POST_TURN_COMPUSH", raising=False)
        _base, options = hook.parse_env()
        assert options.compush is False, (
            f"expected compush to be False but was {options.compush!r}"
        )

    def test_compush_truthy_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POST_TURN_COMPUSH", "yes")
        _base, options = hook.parse_env()
        assert options.compush is True, (
            f"expected compush to be True but was {options.compush!r}"
        )


class TestParseEnvBuildDriver:
    """Tests for build-driver environment parsing."""

    def test_build_driver_defaults_to_auto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("POST_TURN_BUILD_DRIVER", raising=False)
        _base, options = hook.parse_env()
        assert options.build_driver == "auto"

    def test_build_driver_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POST_TURN_BUILD_DRIVER", "netsuke")
        monkeypatch.setenv("POST_TURN_NETSUKE_BIN", "/opt/bin/netsuke")
        monkeypatch.setenv("POST_TURN_MAKE_BIN", "/opt/bin/make")
        _base, options = hook.parse_env()
        assert options.build_driver == "netsuke"
        assert options.netsuke_bin == "/opt/bin/netsuke"
        assert options.make_bin == "/opt/bin/make"


# ---------------------------------------------------------------------------
# run_stop_checks - compush integration
# ---------------------------------------------------------------------------


class TestRunStopChecksCompush:
    """Integration-level tests for compush in run_stop_checks()."""

    driver = driver_mod.BuildDriver("make", "make", "Makefile")

    def test_compush_triggers_after_success(self) -> None:
        """compush=True + quality pass + dirty -> compush_check called."""
        with (
            mock.patch.object(state_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                state_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(state_mod, "merge_base", return_value=("abc123", None)),
            mock.patch.object(
                state_mod, "changed_files", return_value=(["src/foo.py"], None)
            ),
            mock.patch.object(
                state_mod, "select_build_driver", return_value=(self.driver, None)
            ),
            mock.patch.object(state_mod, "evaluate_changes", return_value=0),
            mock.patch.object(
                state_mod, "compush_check", return_value=0
            ) as mock_compush,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            rc = state_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                    compush=True,
                ),
            )
        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        mock_compush.assert_called_once_with(REPO)

    def test_compush_skipped_when_disabled(self) -> None:
        """compush=False -> compush_check not called."""
        with (
            mock.patch.object(state_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                state_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(state_mod, "merge_base", return_value=("abc123", None)),
            mock.patch.object(
                state_mod, "changed_files", return_value=(["src/foo.py"], None)
            ),
            mock.patch.object(
                state_mod, "select_build_driver", return_value=(self.driver, None)
            ),
            mock.patch.object(state_mod, "evaluate_changes", return_value=0),
            mock.patch.object(state_mod, "compush_check") as mock_compush,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            state_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                    compush=False,
                ),
            )
        mock_compush.assert_not_called()

    def test_compush_skipped_on_quality_failure(self) -> None:
        """Quality check failure (nonzero rc) -> compush_check not called."""
        with (
            mock.patch.object(state_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                state_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(state_mod, "merge_base", return_value=("abc123", None)),
            mock.patch.object(
                state_mod, "changed_files", return_value=(["src/foo.py"], None)
            ),
            mock.patch.object(
                state_mod, "select_build_driver", return_value=(self.driver, None)
            ),
            mock.patch.object(state_mod, "evaluate_changes", return_value=1),
            mock.patch.object(state_mod, "compush_check") as mock_compush,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            rc = state_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                    compush=True,
                ),
            )
        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        mock_compush.assert_not_called()

    def test_compush_runs_when_no_files_changed(self) -> None:
        """compush=True still runs when there are no changed files to lint."""
        with (
            mock.patch.object(state_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                state_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(state_mod, "merge_base", return_value=("abc123", None)),
            mock.patch.object(state_mod, "changed_files", return_value=([], None)),
            mock.patch.object(state_mod, "evaluate_changes") as mock_evaluate,
            mock.patch.object(
                state_mod, "compush_check", return_value=0
            ) as mock_compush,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            rc = state_mod.run_stop_checks(
                REPO,
                "origin/main",
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                    compush=True,
                ),
            )
        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        mock_evaluate.assert_not_called()
        mock_compush.assert_called_once_with(REPO)


# ---------------------------------------------------------------------------
# run() - OSError resilience
# ---------------------------------------------------------------------------


class TestRunOSError:
    """Tests for run() handling of OSError (e.g. missing cwd)."""

    def test_nonexistent_cwd_returns_error(self) -> None:
        """run() with a nonexistent cwd returns rc=1 instead of raising."""
        missing_path = Path("/nonexistent/path")
        result = git_mod.run(["git", "status"], missing_path)
        assert result.returncode == 1, (
            f"expected returncode 1 for missing cwd but got {result.returncode!r}"
        )
        assert result.stderr, "expected stderr to describe missing cwd"
        assert str(missing_path) in result.stderr, (
            f"expected missing path in stderr but got {result.stderr!r}"
        )

    def test_run_stop_checks_nonexistent_cwd(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Full pipeline exits cleanly when start_cwd does not exist."""
        with mock.patch("shutil.which", return_value="/usr/bin/git"):
            rc = state_mod.run_stop_checks(
                Path("/nonexistent/path"),
                "origin/main",
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )
        assert rc == 0, f"expected run_stop_checks rc 0 but got {rc!r}"
        assert capsys.readouterr().out == "", (
            "expected no hook output when start_cwd does not exist"
        )


class TestGetMakeTargets:
    """Tests for make target enumeration."""

    def test_make_target_enumeration_does_not_use_query_mode(self) -> None:
        """Target enumeration must not use `make -q`."""
        with mock.patch.object(
            driver_mod,
            "run",
            return_value=subprocess.CompletedProcess(
                args=["make"], returncode=0, stdout="", stderr=""
            ),
        ) as mock_run:
            driver_mod.get_make_targets(REPO)
        mock_run.assert_called_once_with(
            [
                "make",
                "-p",
                "--no-print-directory",
                f"--eval={driver_mod.MAKE_TARGET_PROBE}:",
                driver_mod.MAKE_TARGET_PROBE,
            ],
            REPO,
        )

    def test_missing_make_returns_error(self) -> None:
        """Missing `make` surfaces as an enumeration error."""
        with mock.patch.object(
            driver_mod,
            "run",
            side_effect=FileNotFoundError(2, "No such file or directory", "make"),
        ):
            targets, err = driver_mod.get_make_targets(REPO)
        assert targets is None, (
            f"expected no make targets when make is missing but got {targets!r}"
        )
        assert err == "make not found on PATH", (
            f"expected make-not-found error but got {err!r}"
        )

    def test_make_target_enumeration_does_not_run_default_goal(
        self, tmp_path: Path
    ) -> None:
        """Enumerating targets must not execute recursive default recipes."""
        if shutil.which("make") is None:
            pytest.skip("make not available")

        side_effect = tmp_path / "side-effect"
        (tmp_path / "Makefile").write_text(
            "\n".join([
                ".PHONY: all check-fmt lint",
                "all:",
                f"\t+touch {side_effect}",
                "check-fmt:",
                "lint:",
                "",
            ]),
            encoding="utf-8",
        )

        targets, err = driver_mod.get_make_targets(tmp_path)

        assert err is None
        assert targets is not None
        assert "check-fmt" in targets
        assert "lint" in targets
        assert not side_effect.exists()


class TestBuildDriverSelection:
    """Tests for build-driver selection."""

    def test_auto_prefers_netsuke_when_both_manifests_exist(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "Netsukefile").write_text("actions: {}\n")
        (tmp_path / "Makefile").write_text("all:\n")
        with mock.patch.object(shutil, "which", return_value="/usr/bin/tool"):
            driver, err = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )
        assert err is None
        assert driver is not None
        assert driver.name == "netsuke"

    def test_auto_uses_make_when_only_makefile_exists(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("all:\n")
        with mock.patch.object(shutil, "which", return_value="/usr/bin/make"):
            driver, err = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(always_fetch=False, max_out=12000),
            )
        assert err is None
        assert driver is not None
        assert driver.name == "make"

    def test_make_override_uses_make_when_netsuke_exists(self, tmp_path: Path) -> None:
        (tmp_path / "Netsukefile").write_text("actions: {}\n")
        (tmp_path / "Makefile").write_text("all:\n")
        with mock.patch.object(shutil, "which", return_value="/usr/bin/make"):
            driver, err = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                    build_driver="make",
                ),
            )
        assert err is None
        assert driver is not None
        assert driver.name == "make"

    def test_netsuke_override_errors_when_binary_missing(self, tmp_path: Path) -> None:
        (tmp_path / "Netsukefile").write_text("actions: {}\n")
        with mock.patch.object(shutil, "which", return_value=None):
            driver, err = driver_mod.select_build_driver(
                tmp_path,
                state_mod.StopCheckOptions(
                    always_fetch=False,
                    max_out=12000,
                    build_driver="netsuke",
                ),
            )
        assert driver is None
        assert err is not None
        assert "netsuke not found" in err


class TestNetsukeTargets:
    """Tests for Netsuke target enumeration and execution."""

    def test_parse_netsuke_targets_from_manifest(self) -> None:
        targets = driver_mod.parse_netsuke_targets(
            "\n".join([
                "ninja_required_version = 1.11",
                "build check-fmt: phony",
                "build lint: phony",
                "build markdownlint: phony",
            ])
        )
        assert targets == {"check-fmt", "lint", "markdownlint"}

    def test_markdown_changes_run_netsuke_markdownlint(self) -> None:
        state = state_mod.HookState(changed_files=["docs/users-guide.md"])
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with (
            mock.patch.object(
                state_mod,
                "get_build_targets",
                return_value=({"check-fmt", "lint", "markdownlint"}, None),
            ),
            mock.patch.object(
                state_mod,
                "run_build_targets",
                return_value={
                    "kind": "markdown",
                    "cmd": "netsuke build markdownlint",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                },
            ) as mock_run_targets,
        ):
            rc = state_mod.evaluate_changes(state, REPO, 12000, driver)
        assert rc == 0
        mock_run_targets.assert_called_once_with(
            REPO,
            exec_mod.BuildTargetRequest(driver, "markdown", ["markdownlint"]),
            12000,
        )

    def test_rust_changes_run_netsuke_code_targets(self) -> None:
        state = state_mod.HookState(changed_files=["src/lib.rs"])
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with (
            mock.patch.object(
                state_mod,
                "get_build_targets",
                return_value=({"check-fmt", "lint", "markdownlint"}, None),
            ),
            mock.patch.object(
                state_mod,
                "run_build_targets",
                return_value={
                    "kind": "code",
                    "cmd": "netsuke build check-fmt lint",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                },
            ) as mock_run_targets,
        ):
            rc = state_mod.evaluate_changes(state, REPO, 12000, driver)
        assert rc == 0
        mock_run_targets.assert_called_once_with(
            REPO,
            exec_mod.BuildTargetRequest(driver, "code", ["check-fmt", "lint"]),
            12000,
        )

    def test_python_changes_run_netsuke_typecheck(self) -> None:
        state = state_mod.HookState(changed_files=["src/app.py"])
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with (
            mock.patch.object(
                state_mod,
                "get_build_targets",
                return_value=({"check-fmt", "lint", "typecheck"}, None),
            ),
            mock.patch.object(
                state_mod,
                "run_build_targets",
                return_value={
                    "kind": "code",
                    "cmd": "netsuke build check-fmt lint typecheck",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                },
            ) as mock_run_targets,
        ):
            rc = state_mod.evaluate_changes(state, REPO, 12000, driver)
        assert rc == 0
        mock_run_targets.assert_called_once_with(
            REPO,
            exec_mod.BuildTargetRequest(
                driver, "code", ["check-fmt", "lint", "typecheck"]
            ),
            12000,
        )

    def test_failure_output_uses_build_target_label(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = state_mod.HookState(changed_files=["docs/users-guide.md"])
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        with (
            mock.patch.object(
                state_mod,
                "get_build_targets",
                return_value=({"markdownlint"}, None),
            ),
            mock.patch.object(
                state_mod,
                "run_build_targets",
                return_value={
                    "kind": "markdown",
                    "cmd": "netsuke build markdownlint",
                    "exit_code": 1,
                    "stdout": "bad docs",
                    "stderr": "",
                },
            ),
        ):
            rc = state_mod.evaluate_changes(state, REPO, 12000, driver)
        assert rc == exec_mod.BLOCKED_STATUS
        out = json.loads(capsys.readouterr().out)
        assert "Requested build targets: markdownlint" in out["reason"]
        assert "Command failed (exit 1): netsuke build markdownlint" in out["reason"]


# ---------------------------------------------------------------------------
# truncate
# ---------------------------------------------------------------------------


class TestTruncate:
    """Tests for truncate()."""

    def test_input_shorter_than_max(self) -> None:
        result = exec_mod.truncate("hello", 10)
        assert result == "hello", f"expected unchanged 'hello' but got {result!r}"

    def test_input_exactly_max(self) -> None:
        result = exec_mod.truncate("hello", 5)
        assert result == "hello", f"expected unchanged 'hello' but got {result!r}"

    def test_input_longer_than_max(self) -> None:
        max_chars = 40
        result = exec_mod.truncate("hello world " * 10, max_chars)
        assert len(result) == max_chars, (
            f"expected 40 characters but got {len(result)} ({result!r})"
        )
        assert "... (output truncated) ..." in result, (
            f"expected truncation marker in result but got {result!r}"
        )

    def test_max_zero_returns_empty(self) -> None:
        result = exec_mod.truncate("hello", 0)
        assert result == "", f"expected empty string for max_chars=0 but got {result!r}"


# ---------------------------------------------------------------------------
# default_categories
# ---------------------------------------------------------------------------


class TestDefaultCategories:
    """Tests for default_categories()."""

    def test_all_false(self) -> None:
        cats = state_mod.default_categories()
        assert isinstance(cats, dict), f"expected dict but got {type(cats)}"
        for key, val in cats.items():
            assert isinstance(key, str), f"expected str key but got {type(key)}"
            assert val is False, f"expected {key!r} to be False but was {val!r}"


# ---------------------------------------------------------------------------
# detect_categories
# ---------------------------------------------------------------------------


class TestDetectCategories:
    """Tests for detect_categories()."""

    def test_empty_file_list(self) -> None:
        cats = state_mod.detect_categories([])
        assert all(v is False for v in cats.values()), (
            f"expected all False for empty list but got {cats}"
        )

    def test_py_file(self) -> None:
        cats = state_mod.detect_categories(["src/app.py"])
        assert cats["python_ts"] is True, (
            f"expected python_ts True for .py file but was {cats['python_ts']!r}"
        )
        assert cats["rust"] is False
        assert cats["markdown"] is False

    def test_ts_file(self) -> None:
        cats = state_mod.detect_categories(["src/app.ts"])
        assert cats["python_ts"] is True, (
            f"expected python_ts True for .ts file but was {cats['python_ts']!r}"
        )

    def test_md_file(self) -> None:
        cats = state_mod.detect_categories(["docs/readme.md"])
        assert cats["markdown"] is True, (
            f"expected markdown True for .md file but was {cats['markdown']!r}"
        )
        assert cats["python_ts"] is False

    def test_mixed_py_and_md(self) -> None:
        cats = state_mod.detect_categories(["src/app.py", "docs/readme.md"])
        assert cats["python_ts"] is True
        assert cats["markdown"] is True
        assert cats["rust"] is False


# ---------------------------------------------------------------------------
# dedup_preserve_order
# ---------------------------------------------------------------------------


class TestDedupPreserveOrder:
    """Tests for dedup_preserve_order()."""

    def test_empty_list(self) -> None:
        result = exec_mod.dedup_preserve_order([])
        assert result == [], f"expected empty list but got {result!r}"

    def test_no_duplicates(self) -> None:
        result = exec_mod.dedup_preserve_order(["a", "b", "c"])
        assert result == ["a", "b", "c"], f"expected unchanged list but got {result!r}"

    def test_with_duplicates(self) -> None:
        result = exec_mod.dedup_preserve_order(["a", "b", "a", "c", "b"])
        assert result == ["a", "b", "c"], (
            f"expected duplicates removed but got {result!r}"
        )


# ---------------------------------------------------------------------------
# build_command
# ---------------------------------------------------------------------------


class TestBuildCommand:
    """Tests for build_command()."""

    def test_make_driver(self) -> None:
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        cmd = exec_mod.build_command(driver, ["fmt", "lint"])
        assert cmd == ["make", "--no-print-directory", "fmt", "lint"], (
            f"expected make command but got {cmd!r}"
        )

    def test_netsuke_driver(self) -> None:
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        cmd = exec_mod.build_command(driver, ["build"])
        assert cmd == ["netsuke", "build", "build"], (
            f"expected netsuke build command but got {cmd!r}"
        )


# ---------------------------------------------------------------------------
# parse_bool_env
# ---------------------------------------------------------------------------


class TestParseBoolEnv:
    """Tests for parse_bool_env()."""

    def test_empty_string(self) -> None:
        assert hook.parse_bool_env("") is False

    def test_zero(self) -> None:
        assert hook.parse_bool_env("0") is False

    def test_one(self) -> None:
        assert hook.parse_bool_env("1") is True

    def test_true(self) -> None:
        assert hook.parse_bool_env("true") is True

    def test_yes(self) -> None:
        assert hook.parse_bool_env("yes") is True

    def test_false(self) -> None:
        assert hook.parse_bool_env("false") is False


# ---------------------------------------------------------------------------
# parse_max_output
# ---------------------------------------------------------------------------


class TestParseMaxOutput:
    """Tests for parse_max_output()."""

    def test_valid_integer(self) -> None:
        result = hook.parse_max_output("5000")
        expected = 5000
        assert result == expected, f"expected {expected} but got {result!r}"

    def test_zero(self) -> None:
        result = hook.parse_max_output("0")
        assert result == 0, f"expected 0 but got {result!r}"

    def test_non_integer_returns_default(self) -> None:
        result = hook.parse_max_output("abc", default=999)
        expected = 999
        assert result == expected, f"expected default {expected} but got {result!r}"

    def test_empty_string_returns_default(self) -> None:
        result = hook.parse_max_output("", default=42)
        expected = 42
        assert result == expected, f"expected default {expected} but got {result!r}"


# ---------------------------------------------------------------------------
# parse_hook_input
# ---------------------------------------------------------------------------


class TestParseHookInput:
    """Tests for parse_hook_input()."""

    def test_empty_stdin(self) -> None:
        with mock.patch("sys.stdin.read", return_value=""):
            result = hook.parse_hook_input()
        assert result == {}, f"expected empty dict but got {result!r}"

    def test_valid_json(self) -> None:
        with mock.patch("sys.stdin.read", return_value='{"cwd": "/some/project"}'):
            result = hook.parse_hook_input()
        expected = {"cwd": "/some/project"}
        assert result == expected, f"expected {expected} but got {result!r}"

    def test_invalid_json_returns_empty(self) -> None:
        with mock.patch("sys.stdin.read", return_value="not json"):
            result = hook.parse_hook_input()
        assert result == {}, f"expected empty dict for invalid json but got {result!r}"


# ---------------------------------------------------------------------------
# resolve_start_cwd
# ---------------------------------------------------------------------------


class TestResolveStartCwd:
    """Tests for resolve_start_cwd()."""

    def test_cwd_from_hook_input(self, tmp_path: Path) -> None:
        result = hook.resolve_start_cwd({"cwd": str(tmp_path / "project")})
        assert result == tmp_path / "project", (
            f"expected {tmp_path / 'project'} but got {result!r}"
        )

    def test_claude_project_dir_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "claude-project"))
        result = hook.resolve_start_cwd({"something": "else"})
        assert result == tmp_path / "claude-project", (
            f"expected {tmp_path / 'claude-project'} but got {result!r}"
        )

    def test_falls_back_to_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        result = hook.resolve_start_cwd({})
        assert result == tmp_path, f"expected {tmp_path} but got {result!r}"


# ---------------------------------------------------------------------------
# fail_state
# ---------------------------------------------------------------------------


class TestFailState:
    """Tests for fail_state()."""

    def test_sets_ok_false_and_error(self) -> None:
        state = state_mod.HookState()
        rc = state_mod.fail_state(state, "something went wrong")
        assert state.ok is False, f"expected ok=False but was {state.ok!r}"
        assert state.error == "something went wrong", (
            f"expected error message but got {state.error!r}"
        )
        assert rc == 0, f"expected return 0 but got {rc!r}"

    def test_none_message(self) -> None:
        state = state_mod.HookState()
        state_mod.fail_state(state, None)
        assert state.ok is False
        assert state.error is None, f"expected error=None but got {state.error!r}"


# ---------------------------------------------------------------------------
# format_reason
# ---------------------------------------------------------------------------


class TestFormatReason:
    """Tests for format_reason()."""

    def test_returns_non_empty_string(self) -> None:
        state = state_mod.HookState()
        reason = state_mod.format_reason(state)
        assert isinstance(reason, str), f"expected str but got {type(reason)}"
        assert len(reason) > 0, "expected non-empty reason string"

    def test_contains_base_ref(self) -> None:
        state = state_mod.HookState(base_ref="origin/main")
        reason = state_mod.format_reason(state)
        assert "origin/main" in reason, (
            f"expected base_ref in reason but got {reason!r}"
        )

    def test_contains_error_message(self) -> None:
        state = state_mod.HookState(error="something broke")
        reason = state_mod.format_reason(state)
        assert "something broke" in reason, (
            f"expected error message in reason but got {reason!r}"
        )


# ---------------------------------------------------------------------------
# block_and_print
# ---------------------------------------------------------------------------


class TestBlockAndPrint:
    """Tests for block_and_print()."""

    def test_writes_valid_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        state = state_mod.HookState()
        state_mod.block_and_print(state)
        out = json.loads(capsys.readouterr().out)
        assert isinstance(out, dict), f"expected JSON object but got {type(out)}"

    def test_json_has_decision_and_reason(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = state_mod.HookState()
        state_mod.block_and_print(state)
        out = json.loads(capsys.readouterr().out)
        assert "decision" in out, f"expected 'decision' key in {out}"
        assert "reason" in out, f"expected 'reason' key in {out}"

    def test_decision_is_block(self, capsys: pytest.CaptureFixture[str]) -> None:
        state = state_mod.HookState()
        state_mod.block_and_print(state)
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block", (
            f"expected block decision but got {out['decision']!r}"
        )

    def test_returns_zero(self) -> None:
        state = state_mod.HookState()
        rc = state_mod.block_and_print(state)
        assert rc == 0, f"expected return 0 but got {rc!r}"


# ---------------------------------------------------------------------------
# targets_for_categories
# ---------------------------------------------------------------------------


class TestTargetsForCategories:
    """Tests for targets_for_categories()."""

    def test_all_false_returns_empty(self) -> None:
        cats = {"python_ts": False, "rust": False, "markdown": False}
        result = exec_mod.targets_for_categories(cats)
        assert result == [], f"expected empty list but got {result!r}"

    def test_python_true_returns_targets(self) -> None:
        cats = {"python_ts": True, "rust": False, "markdown": False}
        result = exec_mod.targets_for_categories(cats)
        assert len(result) > 0, f"expected non-empty list but got {result!r}"

    def test_markdown_true_returns_targets(self) -> None:
        cats = {"python_ts": False, "rust": False, "markdown": True}
        result = exec_mod.targets_for_categories(cats)
        assert len(result) > 0, f"expected non-empty list but got {result!r}"

    def test_include_filter(self) -> None:
        cats = {"python_ts": True, "rust": False, "markdown": True}
        result = exec_mod.targets_for_categories(cats, include={"python_ts"})
        assert result == ["check-fmt", "lint", "typecheck"], (
            f"expected only python targets but got {result!r}"
        )


# ---------------------------------------------------------------------------
# main() end-to-end
# ---------------------------------------------------------------------------


class TestMain:
    """End-to-end tests for main()."""

    def test_exits_zero_outside_repo(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """main() returns 0 with empty stdout when CWD is not a git repo."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.stdin.read", lambda: "")
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        with mock.patch("shutil.which", return_value="/usr/bin/git"):
            rc = hook.main()
        assert rc == 0, f"expected exit 0 but got {rc!r}"
        assert capsys.readouterr().out == "", "expected no output outside repo"

    def test_exits_zero_no_stdin(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """main() returns 0 with empty stdout when stdin is empty."""
        monkeypatch.setattr("sys.stdin.read", lambda: "")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "nonexistent"))
        with (
            mock.patch("shutil.which", return_value="/usr/bin/git"),
            mock.patch.object(git_mod, "repo_root", return_value=(None, "not a repo")),
        ):
            rc = hook.main()
        assert rc == 0, f"expected exit 0 but got {rc!r}"
        assert capsys.readouterr().out == "", "expected no output with no stdin"


# ---------------------------------------------------------------------------
# get_netsuke_targets
# ---------------------------------------------------------------------------


class TestGetNetsukeTargets:
    """Tests for get_netsuke_targets()."""

    def test_returns_targets(self) -> None:
        """Successful netsuke manifest call -> parsed target set."""
        with mock.patch.object(driver_mod, "run") as mock_run:
            mock_run.return_value = _completed(
                0,
                stdout="\n".join([
                    "ninja_required_version = 1.11",
                    "build check-fmt: phony",
                    "build lint: phony",
                ]),
            )
            targets, err = driver_mod.get_netsuke_targets(REPO)
        assert targets == {"check-fmt", "lint"}, (
            f"expected target set but got {targets!r}"
        )
        assert err is None, f"expected no error but got {err!r}"
        mock_run.assert_called_once_with(["netsuke", "manifest", "-"], REPO)

    def test_returns_error_on_failure(self) -> None:
        """Non-zero exit from netsuke -> error message."""
        with mock.patch.object(driver_mod, "run") as mock_run:
            mock_run.return_value = _completed(1, stderr="netsuke: not found")
            targets, err = driver_mod.get_netsuke_targets(REPO)
        assert targets is None, f"expected no targets on failure but got {targets!r}"
        assert "netsuke: not found" in (err or ""), (
            f"expected error message but got {err!r}"
        )
        mock_run.assert_called_once_with(["netsuke", "manifest", "-"], REPO)

    def test_returns_error_when_executable_missing(self) -> None:
        """FileNotFoundError -> error message, no targets."""
        with mock.patch.object(
            driver_mod,
            "run",
            side_effect=FileNotFoundError(2, "No such file or directory", "netsuke"),
        ):
            targets, err = driver_mod.get_netsuke_targets(REPO)
        assert targets is None, (
            f"expected no targets when netsuke is missing but got {targets!r}"
        )
        assert "netsuke not found on PATH" in (err or ""), (
            f"expected missing-executable error but got {err!r}"
        )


# ---------------------------------------------------------------------------
# changed_files
# ---------------------------------------------------------------------------


class TestChangedFiles:
    """Tests for changed_files()."""

    def test_returns_sorted_union(self) -> None:
        """Three non-overlapping file lists -> sorted union."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout="z.py\nb.py\n"),  # git diff
                _completed(0, stdout="c.py\n"),  # git diff --cached
                _completed(0, stdout="a.py\n"),  # git ls-files
            ]
            files, err = git_mod.changed_files(REPO, "abc123")
        assert err is None, f"expected no error but got {err!r}"
        assert files == ["a.py", "b.py", "c.py", "z.py"], (
            f"expected sorted union but got {files!r}"
        )

    def test_deduplicates_across_sources(self) -> None:
        """Same file from two sources -> appears once."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout="x.py\n"),
                _completed(0, stdout="x.py\n"),
                _completed(0, stdout=""),
            ]
            files, err = git_mod.changed_files(REPO, "abc123")
        assert err is None, f"expected no error but got {err!r}"
        assert files == ["x.py"], f"expected deduplicated list but got {files!r}"

    def test_returns_empty_on_no_changes(self) -> None:
        """All three sources empty -> empty list."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout=""),
                _completed(0, stdout=""),
                _completed(0, stdout=""),
            ]
            files, err = git_mod.changed_files(REPO, "abc123")
        assert err is None, f"expected no error but got {err!r}"
        assert files == [], f"expected empty list but got {files!r}"

    def test_asserts_git_diff_command(self) -> None:
        """First run() call uses git diff --name-only <base>."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout=""),
                _completed(0, stdout=""),
                _completed(0, stdout=""),
            ]
            git_mod.changed_files(REPO, "abc123")
        assert mock_run.call_args_list[0] == mock.call(
            ["git", "diff", "--name-only", "abc123"], REPO
        )


# ---------------------------------------------------------------------------
# merge_base
# ---------------------------------------------------------------------------


class TestMergeBase:
    """Tests for merge_base()."""

    def test_returns_commit_hash(self) -> None:
        """Successful merge-base -> stripped commit hash."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(
                0, stdout="abc123def4567890abc123def4567890abc123\n"
            )
            commit, err = git_mod.merge_base(REPO, "origin/main")
        assert commit == "abc123def4567890abc123def4567890abc123", (
            f"expected commit hash but got {commit!r}"
        )
        assert err is None, f"expected no error but got {err!r}"

    def test_returns_none_on_failure(self) -> None:
        """Non-zero exit from merge-base -> None + error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(1, stderr="fatal: not a thing")
            commit, err = git_mod.merge_base(REPO, "origin/main")
        assert commit is None, f"expected None on failure but got {commit!r}"
        assert "fatal: not a thing" in (err or ""), (
            f"expected error message but got {err!r}"
        )

    def test_asserts_merge_base_command(self) -> None:
        """run() is called with the correct merge-base command."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="abc\n")
            git_mod.merge_base(REPO, "origin/main")
        mock_run.assert_called_once_with(
            ["git", "merge-base", "origin/main", "HEAD"], REPO
        )

    def test_empty_stdout_returns_none(self) -> None:
        """Empty stdout -> None + error."""
        with mock.patch.object(git_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="")
            commit, err = git_mod.merge_base(REPO, "origin/main")
        assert commit is None, f"expected None for empty output but got {commit!r}"
        assert err is not None, "expected an error for empty merge-base output"


# ---------------------------------------------------------------------------
# run_build_targets
# ---------------------------------------------------------------------------


class TestRunBuildTargets:
    """Tests for run_build_targets()."""

    def test_returns_command_result(self) -> None:
        """Successful run -> a CommandResult dict."""
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        request = exec_mod.BuildTargetRequest(driver, "code", ["check-fmt", "lint"])
        with mock.patch.object(exec_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="ok\n", stderr="")
            result = exec_mod.run_build_targets(REPO, request, 12000)
        assert result["kind"] == "code", (
            f"expected kind code but got {result['kind']!r}"
        )
        assert result["exit_code"] == 0, (
            f"expected exit_code 0 but got {result['exit_code']}"
        )
        assert "ok" in result["stdout"], (
            f"expected stdout to contain ok but got {result['stdout']!r}"
        )

    def test_passes_correct_make_command(self) -> None:
        """Make driver -> [executable, --no-print-directory, targets...]."""
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        request = exec_mod.BuildTargetRequest(driver, "code", ["check-fmt", "lint"])
        with mock.patch.object(exec_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="", stderr="")
            exec_mod.run_build_targets(REPO, request, 12000)
        mock_run.assert_called_once_with(
            ["make", "--no-print-directory", "check-fmt", "lint"], REPO
        )

    def test_passes_correct_netsuke_command(self) -> None:
        """Netsuke driver -> [executable, build, targets...]."""
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        request = exec_mod.BuildTargetRequest(driver, "code", ["check-fmt", "lint"])
        with mock.patch.object(exec_mod, "run") as mock_run:
            mock_run.return_value = _completed(0, stdout="", stderr="")
            exec_mod.run_build_targets(REPO, request, 12000)
        mock_run.assert_called_once_with(
            ["netsuke", "build", "check-fmt", "lint"], REPO
        )

    def test_captures_stdout_and_stderr(self) -> None:
        """Non-empty stdout and stderr -> both present in result."""
        driver = driver_mod.BuildDriver("netsuke", "netsuke", "Netsukefile")
        request = exec_mod.BuildTargetRequest(driver, "code", ["check-fmt"])
        with mock.patch.object(exec_mod, "run") as mock_run:
            mock_run.return_value = _completed(
                0, stdout="all good\n", stderr="warning: something\n"
            )
            result = exec_mod.run_build_targets(REPO, request, 12000)
        assert result["stdout"] == "all good\n", (
            f"expected stdout but got {result['stdout']!r}"
        )
        assert "warning: something" in result["stderr"], (
            f"expected stderr but got {result['stderr']!r}"
        )

    def test_empty_targets_skips_run(self) -> None:
        """Empty target list -> skips subprocess, returns sentinel result."""
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        request = exec_mod.BuildTargetRequest(driver, "code", [])
        with mock.patch.object(exec_mod, "run") as mock_run:
            result = exec_mod.run_build_targets(REPO, request, 12000)
        mock_run.assert_not_called()
        assert result["exit_code"] == 0, (
            f"expected exit_code 0 but got {result['exit_code']}"
        )
        assert result["cmd"] == "", f"expected empty cmd but got {result['cmd']!r}"

    def test_handles_file_not_found(self) -> None:
        """FileNotFoundError from run -> exit_code 127, error in stderr."""
        driver = driver_mod.BuildDriver("make", "make", "Makefile")
        request = exec_mod.BuildTargetRequest(driver, "code", ["check-fmt"])
        with mock.patch.object(
            exec_mod,
            "run",
            side_effect=FileNotFoundError(2, "No such file or directory", "make"),
        ):
            result = exec_mod.run_build_targets(REPO, request, 12000)
        enoent_exit = 127
        assert result["exit_code"] == enoent_exit, (
            f"expected exit_code {enoent_exit} but got {result['exit_code']}"
        )
        assert "make not found" in result["stderr"], (
            f"expected not-found error but got {result['stderr']!r}"
        )
