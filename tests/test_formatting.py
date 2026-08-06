"""Exercise blocking-reason formatting for failed quality checks.

These tests kill the ``formatting`` failure-render survivors tracked in
#36.
"""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook import formatting as formatting_mod
from post_turn_quality_stop_hook.state import HookState

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook.execution import CommandResult


def _command(**overrides: object) -> CommandResult:
    """Build a CommandResult from defaults, applying keyword overrides."""
    base: dict[str, object] = {
        "kind": "code",
        "cmd": "make lint",
        "exit_code": 1,
        "stdout": "",
        "stderr": "",
    }
    base.update(overrides)
    return typ.cast("CommandResult", base)


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

    def test_missing_keys_use_defaults(self) -> None:
        """Absent cmd/exit_code keys fall back to empty and ``?``."""
        # A partial payload exercises the .get() defaults; cast past the
        # TypedDict contract to model a malformed CommandResult.
        empty = typ.cast("CommandResult", {})
        result = formatting_mod._format_command_failure(empty)
        assert result[1] == "Command failed (exit ?): ", (
            f"expected default cmd/exit_code but got {result!r}"
        )
        assert result[3] == "(no output captured)"


class TestFormatReason:
    """Golden and filter tests for format_reason()."""

    def test_golden_failure_reason(self) -> None:
        """A representative failing state renders the exact blocking reason."""
        state = HookState(
            ok=False,
            base_ref="origin/main",
            base_commit="abc1234",
            changed_files=["src/a.py"],
            categories={"code"},
            commands=[
                _command(cmd="make lint", exit_code=1, stdout="boom", stderr="err!")
            ],
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
            commands=[
                _command(cmd="make good", exit_code=0, stdout="ok"),
                _command(cmd="make bad", exit_code=1, stderr="nope"),
            ],
        )
        reason = formatting_mod.format_reason(state)
        assert "Command failed (exit 1): make bad" in reason
        assert "make good" not in reason, "passing command must not be reported"
