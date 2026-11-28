"""Exercise blocking-reason formatting for failed quality checks.

These tests kill the ``formatting`` failure-render survivors tracked in
#36.
"""

from __future__ import annotations

from pathlib import Path

from post_turn_quality_stop_hook import formatting as formatting_mod
from post_turn_quality_stop_hook.disk_space import LowSpace
from post_turn_quality_stop_hook.execution import CommandResult
from post_turn_quality_stop_hook.state import HookState


def _command(
    *,
    cmd: str = "make lint",
    exit_code: int = 1,
    stdout: str = "",
    stderr: str = "",
) -> CommandResult:
    """Build a CommandResult from defaults, applying keyword overrides."""
    return CommandResult(
        kind="code",
        cmd=cmd,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
    )


class TestFormatCommandFailure:
    """Tests for _format_command_failure()."""

    def test_both_streams_combined(self) -> None:
        """Stdout and stderr join with a newline, stripped, and fenced."""
        result = formatting_mod._format_command_failure(
            _command(exit_code=2, stdout="out\n", stderr="err\n")
        )
        assert result == [
            "",
            "Command failed (exit 2): make lint",
            "```",
            "out\n\nerr",
            "```",
        ], f"unexpected failure block: {result!r}"

    def test_stdout_only(self) -> None:
        """With only stdout, stderr is omitted from the combined block."""
        result = formatting_mod._format_command_failure(
            _command(stdout="just out\n", stderr="")
        )
        assert result[3] == "just out", f"expected stdout-only body but got {result!r}"

    def test_stderr_only(self) -> None:
        """With only stderr, stdout is omitted from the combined block."""
        result = formatting_mod._format_command_failure(
            _command(stdout="", stderr="just err\n")
        )
        assert result[3] == "just err", f"expected stderr-only body but got {result!r}"

    def test_no_output_captured(self) -> None:
        """Empty stdout and stderr yield the explicit placeholder."""
        result = formatting_mod._format_command_failure(_command(stdout="", stderr=""))
        assert result[3] == "(no output captured)", (
            f"expected placeholder but got {result!r}"
        )


class TestFormatReason:
    """Golden and filter tests for format_reason()."""

    def test_golden_failure_reason(self) -> None:
        """A representative failing state renders the exact blocking reason."""
        state = HookState(
            ok=False,
            base_ref="origin/main",
            base_commit="abc1234",
            changed_files=("src/a.py",),
            categories=frozenset({"code"}),
            commands=(
                _command(cmd="make lint", exit_code=1, stdout="boom", stderr="err!"),
            ),
        )
        expected = "\n".join([
            "Post-turn checks failed.",
            "",
            "Diff base: origin/main (abc1234)",
            "",
            "Changed files vs origin/main: 1",
            "- src/a.py",
            "",
            "Detected change types: Code",
            "",
            "Command failed (exit 1): make lint",
            "```",
            "boom\nerr!",
            "```",
            "",
            (
                "Fix the failures above. The checks will re-run at the end of "
                "the next turn."
            ),
        ])
        assert formatting_mod.format_reason(state) == expected

    def test_only_failing_commands_are_reported(self) -> None:
        """Commands with exit_code 0 are filtered out; non-zero ones remain."""
        state = HookState(
            base_ref="origin/main",
            base_commit="abc1234",
            commands=(
                _command(cmd="make good", exit_code=0, stdout="ok"),
                _command(cmd="make bad", exit_code=1, stderr="nope"),
            ),
        )
        reason = formatting_mod.format_reason(state)
        assert "Command failed (exit 1): make bad" in reason
        assert "make good" not in reason, "passing command must not be reported"


# ---------------------------------------------------------------------------
# Skip notices

# ---------------------------------------------------------------------------


class TestLowSpaceNotice:
    """Tests for low_space_notice()."""

    def test_notice_names_each_volume_and_its_free_space(self) -> None:
        """Every filled volume is reported with its measured free space."""
        notice = formatting_mod.low_space_notice((
            LowSpace(path=Path("/data"), free_bytes=0),
            LowSpace(path=Path("/scratch"), free_bytes=1024),
        ))

        assert notice.startswith("quality gates skipped: "), (
            f"expected the skip prefix but got {notice!r}"
        )
        assert "/data has 0 B free" in notice, (
            f"expected /data's measured space but got {notice!r}"
        )
        assert "/scratch has 1.0 KiB free" in notice, (
            f"expected /scratch's measured space but got {notice!r}"
        )
        assert "free disk space and re-run" in notice, (
            f"expected the operator instruction but got {notice!r}"
        )

    def test_notice_is_a_single_line(self) -> None:
        """The notice stays on one stderr line for log collectors."""
        notice = formatting_mod.low_space_notice((
            LowSpace(path=Path("/data"), free_bytes=0),
        ))

        assert "\n" not in notice, f"expected one line but got {notice!r}"


class TestDiskExhaustionNotice:
    """Tests for disk_exhaustion_notice()."""

    def test_notice_names_the_groups_and_repository(self) -> None:
        """The notice names every affected group and the repository."""
        notice = formatting_mod.disk_exhaustion_notice(
            repo=Path("/repo"),
            groups=("code", "markdown"),
        )

        assert notice.startswith("quality gates skipped: "), (
            f"expected the skip prefix but got {notice!r}"
        )
        assert "code, markdown" in notice, (
            f"expected both affected groups but got {notice!r}"
        )
        assert "/repo" in notice, f"expected the repository but got {notice!r}"
        assert "No space left on device" in notice, (
            f"expected the cause but got {notice!r}"
        )
        assert "free disk space and re-run" in notice, (
            f"expected the operator instruction but got {notice!r}"
        )
        assert "\n" not in notice, f"expected one line but got {notice!r}"
