"""Exercise blocking-reason formatting for failed quality checks.

These tests kill the ``formatting`` failure-render survivors tracked in
#36.
"""

from __future__ import annotations

import json

import pytest

from post_turn_quality_stop_hook import formatting as formatting_mod
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


class TestBlockPayloadEmission:
    """The one emitter declares the hook's blocking payload shape."""

    def test_emit_block_payload_writes_the_hook_contract(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A rendered reason becomes the documented blocking JSON."""
        formatting_mod.emit_block_payload("please fix the build")

        payload = json.loads(capsys.readouterr().out)
        assert payload == {"decision": "block", "reason": "please fix the build"}

    def test_the_reason_is_emitted_verbatim(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The emitter adds no prefix or escaping beyond JSON encoding."""
        reason = "first line\nsecond line"

        formatting_mod.emit_block_payload(reason)

        assert json.loads(capsys.readouterr().out)["reason"] == reason
