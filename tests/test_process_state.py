"""Exercise the ``/proc/<pid>/stat`` probe used for process liveness."""

from __future__ import annotations

import pytest
from proc_stat import process_state

# ---------------------------------------------------------------------------
# process_state
# ---------------------------------------------------------------------------


class TestProcessState:
    """Tests for the ``/proc/<pid>/stat`` probe used by ``_is_alive``."""

    @pytest.mark.parametrize(
        ("stat_line", "expected"),
        [
            ("1234 (git) S 1 1234 1234 0 -1 4194560", "S"),
            ("1234 (my worker) Z 1 1234 1234 0 -1 4194560", "Z"),
            ("1234 (a (b) c) R 1 1234 1234 0 -1 4194560", "R"),
        ],
    )
    def test_reads_the_state_after_the_command_name(
        self, stat_line: str, expected: str
    ) -> None:
        """Spaces and parentheses inside ``comm`` must not shift the state."""
        assert process_state(stat_line) == expected, (
            f"process_state({stat_line!r}) must yield the state {expected!r}"
        )

    def test_returns_none_for_an_unparsable_line(self) -> None:
        """A line without a command name yields no state."""
        assert process_state("not-a-stat-line") is None, (
            "a line with no closing parenthesis must not yield a state"
        )
        assert process_state("1234 (git)") is None, (
            "a line ending after the command name must not yield a state"
        )
