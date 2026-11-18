"""Exercise the ``/proc/<pid>/stat`` probe used for process liveness."""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _process_state(stat_line: str) -> str | None:
    """Return the state letter from one ``/proc/<pid>/stat`` line.

    The line reads ``pid (comm) state ...``, and ``comm`` is the process'
    self-chosen name, which may contain spaces and parentheses. Splitting the
    line on whitespace therefore cannot locate the state field; it starts
    after the final ``)``. ``None`` means the line could not be parsed.
    """
    close = stat_line.rfind(")")
    if close == -1:
        return None
    remainder = stat_line[close + 1 :].split()
    return remainder[0] if remainder else None


requires_proc_stat = pytest.mark.skipif(
    not Path("/proc/self/stat").is_file(), reason="requires Linux /proc"
)

# ---------------------------------------------------------------------------
# _process_state
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
        assert _process_state(stat_line) == expected

    def test_returns_none_for_an_unparsable_line(self) -> None:
        """A line without a command name yields no state."""
        assert _process_state("not-a-stat-line") is None
        assert _process_state("1234 (git)") is None
