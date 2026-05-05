"""Exercise stop-hook pipeline: evaluate_changes, compush_check, blocking."""

from __future__ import annotations

import json
import subprocess  # noqa: S404
from pathlib import Path
from unittest import mock

import pytest

from post_turn_quality_stop_hook import driver as driver_mod
from post_turn_quality_stop_hook import formatting as formatting_mod
from post_turn_quality_stop_hook import pipeline as pipeline_mod
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


class TestCompushCheck:
    """Tests for compush_check()."""

    def test_dirty_with_upstream(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Dirty tree + upstream -> block with push message."""
        with (
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(True, None)
            ),
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
        ):
            rc = pipeline_mod.compush_check(REPO)
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
                pipeline_mod, "has_uncommitted_changes", return_value=(True, None)
            ),
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=(None, "no upstream")
            ),
        ):
            rc = pipeline_mod.compush_check(REPO)
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
                pipeline_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(
                pipeline_mod, "has_unpushed_commits", return_value=(False, None)
            ),
        ):
            rc = pipeline_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        assert capsys.readouterr().out == "", "expected no hook output for clean tree"

    def test_error_checking_changes(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Error from has_uncommitted_changes -> silent exit 0."""
        with (
            mock.patch.object(
                pipeline_mod, "get_upstream_ref", return_value=("origin/main", None)
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(None, "oops")
            ),
        ):
            rc = pipeline_mod.compush_check(REPO)
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
                pipeline_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(
                pipeline_mod, "has_unpushed_commits", return_value=(True, None)
            ),
        ):
            rc = pipeline_mod.compush_check(REPO)
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
                pipeline_mod, "get_upstream_ref", return_value=(None, "no upstream")
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(pipeline_mod, "has_unpushed_commits") as mock_ahead,
        ):
            rc = pipeline_mod.compush_check(REPO)
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
                pipeline_mod, "get_upstream_ref", return_value=("origin/feature", None)
            ),
            mock.patch.object(
                pipeline_mod, "has_uncommitted_changes", return_value=(False, None)
            ),
            mock.patch.object(
                pipeline_mod, "has_unpushed_commits", return_value=(None, "oops")
            ),
        ):
            rc = pipeline_mod.compush_check(REPO)
        assert rc == 0, f"expected compush_check rc 0 but got {rc!r}"
        assert capsys.readouterr().out == "", (
            "expected no hook output when ahead check errors are suppressed"
        )


# ---------------------------------------------------------------------------
# parse_env - compush flag

# ---------------------------------------------------------------------------


class TestRunStopChecksCompush:
    """Integration-level tests for compush in run_stop_checks()."""

    driver = driver_mod.BuildDriver("make", "make", "Makefile")

    def test_compush_triggers_after_success(self) -> None:
        """compush=True + quality pass + dirty -> compush_check called."""
        with (
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(
                pipeline_mod, "changed_files", return_value=(["src/foo.py"], None)
            ),
            mock.patch.object(
                pipeline_mod, "select_build_driver", return_value=(self.driver, None)
            ),
            mock.patch.object(pipeline_mod, "evaluate_changes", return_value=0),
            mock.patch.object(
                pipeline_mod, "compush_check", return_value=0
            ) as mock_compush,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            rc = pipeline_mod.run_stop_checks(
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
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(
                pipeline_mod, "changed_files", return_value=(["src/foo.py"], None)
            ),
            mock.patch.object(
                pipeline_mod, "select_build_driver", return_value=(self.driver, None)
            ),
            mock.patch.object(pipeline_mod, "evaluate_changes", return_value=0),
            mock.patch.object(pipeline_mod, "compush_check") as mock_compush,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            pipeline_mod.run_stop_checks(
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
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(
                pipeline_mod, "changed_files", return_value=(["src/foo.py"], None)
            ),
            mock.patch.object(
                pipeline_mod, "select_build_driver", return_value=(self.driver, None)
            ),
            mock.patch.object(pipeline_mod, "evaluate_changes", return_value=1),
            mock.patch.object(pipeline_mod, "compush_check") as mock_compush,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            rc = pipeline_mod.run_stop_checks(
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
            mock.patch.object(pipeline_mod, "repo_root", return_value=(REPO, None)),
            mock.patch.object(
                pipeline_mod, "ensure_base_ref", return_value=(True, None, False)
            ),
            mock.patch.object(
                pipeline_mod, "merge_base", return_value=("abc123", None)
            ),
            mock.patch.object(pipeline_mod, "changed_files", return_value=([], None)),
            mock.patch.object(pipeline_mod, "evaluate_changes") as mock_evaluate,
            mock.patch.object(
                pipeline_mod, "compush_check", return_value=0
            ) as mock_compush,
            mock.patch("shutil.which", return_value="/usr/bin/git"),
        ):
            rc = pipeline_mod.run_stop_checks(
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


class TestFailState:
    """Tests for fail_state()."""

    def test_sets_ok_false_and_error(self) -> None:
        state = state_mod.HookState()
        rc = formatting_mod.fail_state(state, "something went wrong")
        assert state.ok is False, f"expected ok=False but was {state.ok!r}"
        assert state.error == "something went wrong", (
            f"expected error message but got {state.error!r}"
        )
        assert rc == 0, f"expected return 0 but got {rc!r}"

    def test_none_message(self) -> None:
        state = state_mod.HookState()
        formatting_mod.fail_state(state, None)
        assert state.ok is False
        assert state.error is None, f"expected error=None but got {state.error!r}"


# ---------------------------------------------------------------------------
# format_reason

# ---------------------------------------------------------------------------


class TestFormatReason:
    """Tests for format_reason()."""

    def test_returns_non_empty_string(self) -> None:
        state = state_mod.HookState()
        reason = formatting_mod.format_reason(state)
        assert isinstance(reason, str), f"expected str but got {type(reason)}"
        assert len(reason) > 0, "expected non-empty reason string"

    def test_contains_base_ref(self) -> None:
        state = state_mod.HookState(base_ref="origin/main")
        reason = formatting_mod.format_reason(state)
        assert "origin/main" in reason, (
            f"expected base_ref in reason but got {reason!r}"
        )

    def test_contains_error_message(self) -> None:
        state = state_mod.HookState(error="something broke")
        reason = formatting_mod.format_reason(state)
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
        formatting_mod.block_and_print(state)
        out = json.loads(capsys.readouterr().out)
        assert isinstance(out, dict), f"expected JSON object but got {type(out)}"

    def test_json_has_decision_and_reason(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = state_mod.HookState()
        formatting_mod.block_and_print(state)
        out = json.loads(capsys.readouterr().out)
        assert "decision" in out, f"expected 'decision' key in {out}"
        assert "reason" in out, f"expected 'reason' key in {out}"

    def test_decision_is_block(self, capsys: pytest.CaptureFixture[str]) -> None:
        state = state_mod.HookState()
        formatting_mod.block_and_print(state)
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block", (
            f"expected block decision but got {out['decision']!r}"
        )

    def test_returns_zero(self) -> None:
        state = state_mod.HookState()
        rc = formatting_mod.block_and_print(state)
        assert rc == 0, f"expected return 0 but got {rc!r}"


# ---------------------------------------------------------------------------
# targets_for_categories
