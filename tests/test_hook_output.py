"""Unit tests for the adapter between printed hook JSON and the breaker."""

from __future__ import annotations

import json

import pytest

from post_turn_quality_stop_hook import hook_output as ho
from post_turn_quality_stop_hook.failure_breaker import CheckOutcome, CheckResult

BLOCK_PAYLOAD = json.dumps({"decision": "block", "reason": "broken"})
SENTINEL_EXIT_CODE = 3


class TestBlockDetection:
    """Recognize blocking hook output."""

    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            ("", False),
            (BLOCK_PAYLOAD + "\n", True),
            ('{"decision": "approve"}\n', False),
            ("not json\n" + BLOCK_PAYLOAD + "\n", True),
            ("[1, 2]\n", False),
        ],
    )
    def test_is_block_output(self, output: str, *, expected: bool) -> None:
        """Only a JSON object with a block decision counts as a failure."""
        assert ho.is_block_output(output) is expected


class TestCaptureCheckResult:
    """Translate printed decisions into a ``CheckResult``."""

    def test_a_printed_block_is_a_blocked_outcome(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The block is forwarded and reported as ``BLOCKED``."""

        def run() -> int:
            """Print a blocking decision as the stop checks would."""
            print(BLOCK_PAYLOAD)
            return 0

        assert ho.capture_check_result(run) == CheckResult(0, CheckOutcome.BLOCKED)
        assert capsys.readouterr().out == BLOCK_PAYLOAD + "\n"

    def test_other_output_and_the_exit_code_pass_through(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Non-blocking output is forwarded unchanged with its exit code."""

        def run() -> int:
            """Print a marker line and return a distinctive code."""
            print("hello")
            return SENTINEL_EXIT_CODE

        assert ho.capture_check_result(run) == CheckResult(
            SENTINEL_EXIT_CODE, CheckOutcome.PASSED
        )
        assert capsys.readouterr().out == "hello\n"

    def test_output_is_forwarded_when_the_checks_raise(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Captured output is not lost when the wrapped run fails."""

        def run() -> int:
            """Print a line and then fail unexpectedly."""
            print("partial")
            message = "boom"
            raise RuntimeError(message)

        with pytest.raises(RuntimeError, match="boom"):
            ho.capture_check_result(run)

        assert capsys.readouterr().out == "partial\n"


def test_short_circuit_message_allows_the_stop_and_explains() -> None:
    """The message is a non-blocking systemMessage naming the streak."""
    payload = json.loads(ho.short_circuit_message(SENTINEL_EXIT_CODE))

    assert "decision" not in payload
    assert f"{SENTINEL_EXIT_CODE} consecutive" in payload["systemMessage"]
