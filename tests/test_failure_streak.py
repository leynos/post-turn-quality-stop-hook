"""Unit tests for the consecutive-failure circuit breaker."""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

import pytest

from post_turn_quality_stop_hook import failure_streak as fs

if typ.TYPE_CHECKING:
    import collections.abc as cabc

BLOCK_PAYLOAD = json.dumps({"decision": "block", "reason": "broken"})
LIMIT = 3
SMALL_LIMIT = 2
SENTINEL_EXIT_CODE = 3
STORED_COUNT = 4


def _blocking_run() -> int:
    """Emit a blocking decision as the stop checks would."""
    print(BLOCK_PAYLOAD)
    return 0


def _passing_run() -> int:
    """Pass silently as the stop checks would."""
    return 0


class _CountingRun:
    """Record how often the wrapped checks actually run."""

    def __init__(self, run: cabc.Callable[[], int]) -> None:
        """Wrap ``run`` with a zero call count."""
        self.calls = 0
        self._run = run

    def __call__(self) -> int:
        """Count the call and delegate to the wrapped run."""
        self.calls += 1
        return self._run()


class TestStateRoot:
    """Resolve the XDG state directory."""

    def test_uses_absolute_xdg_state_home(self, tmp_path: Path) -> None:
        """An absolute XDG_STATE_HOME is honoured."""
        root = fs.state_root({"XDG_STATE_HOME": str(tmp_path)})
        assert root == tmp_path / "post-turn-quality-stop-hook" / "failure-streaks"

    @pytest.mark.parametrize("value", ["", "relative/state"])
    def test_ignores_empty_or_relative_xdg_state_home(
        self, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The XDG spec says empty and relative values must be ignored."""
        monkeypatch.setenv("HOME", str(tmp_path))
        root = fs.state_root({"XDG_STATE_HOME": value})
        expected = (
            tmp_path / ".local" / "state" / "post-turn-quality-stop-hook"
        ) / "failure-streaks"
        assert root == expected


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
        assert fs.is_block_output(output) is expected


class TestRunWithFailureLimit:
    """Short-circuit the checks after repeated blocks in one directory."""

    def test_counts_blocks_and_short_circuits_at_the_limit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """After ``limit`` blocks the next run skips the checks and allows stop."""
        directory = tmp_path / "work"
        root = tmp_path / "state"
        run = _CountingRun(_blocking_run)

        for _ in range(LIMIT):
            fs.run_with_failure_limit(directory, LIMIT, run, state_dir=root)
            assert json.loads(capsys.readouterr().out)["decision"] == "block"

        rc = fs.run_with_failure_limit(directory, LIMIT, run, state_dir=root)

        assert rc == 0
        assert run.calls == LIMIT, "the short-circuited run must not execute the checks"
        payload = json.loads(capsys.readouterr().out)
        assert "decision" not in payload
        assert f"{LIMIT} consecutive" in payload["systemMessage"]

    def test_short_circuit_resets_the_streak(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Checks resume after a short circuit rather than staying disabled."""
        directory = tmp_path / "work"
        root = tmp_path / "state"
        run = _CountingRun(_blocking_run)

        for _ in range(SMALL_LIMIT + 1):  # block, block, short circuit
            fs.run_with_failure_limit(directory, SMALL_LIMIT, run, state_dir=root)
        capsys.readouterr()
        fs.run_with_failure_limit(directory, SMALL_LIMIT, run, state_dir=root)

        assert run.calls == SMALL_LIMIT + 1
        assert json.loads(capsys.readouterr().out)["decision"] == "block"

    def test_success_clears_the_streak(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A passing run breaks the chain of consecutive failures."""
        directory = tmp_path / "work"
        root = tmp_path / "state"
        blocking = _CountingRun(_blocking_run)

        fs.run_with_failure_limit(directory, SMALL_LIMIT, blocking, state_dir=root)
        fs.run_with_failure_limit(directory, SMALL_LIMIT, _passing_run, state_dir=root)
        for _ in range(SMALL_LIMIT):
            capsys.readouterr()
            fs.run_with_failure_limit(directory, SMALL_LIMIT, blocking, state_dir=root)

        assert blocking.calls == SMALL_LIMIT + 1
        assert json.loads(capsys.readouterr().out)["decision"] == "block"
        streak = fs.read_streak(fs.streak_path(directory, root))
        assert streak == SMALL_LIMIT

    def test_directories_are_counted_independently(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A streak in one directory does not short-circuit another."""
        root = tmp_path / "state"
        run = _CountingRun(_blocking_run)

        directories = [tmp_path / "a", tmp_path / "b"]
        for directory in directories:
            fs.run_with_failure_limit(directory, 1, run, state_dir=root)

        assert run.calls == len(directories)
        outputs = capsys.readouterr().out.splitlines()
        assert all(json.loads(line)["decision"] == "block" for line in outputs)

    def test_zero_limit_disables_the_breaker(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A limit of zero never short-circuits and writes no state."""
        root = tmp_path / "state"
        run = _CountingRun(_blocking_run)

        for _ in range(LIMIT):
            fs.run_with_failure_limit(tmp_path, 0, run, state_dir=root)

        assert run.calls == LIMIT
        assert len(capsys.readouterr().out.splitlines()) == LIMIT
        assert not root.exists()

    def test_passes_output_and_exit_code_through(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The wrapper forwards the checks' output and return code unchanged."""

        def run() -> int:
            """Print a marker line and return a distinctive code."""
            print("hello")
            return SENTINEL_EXIT_CODE

        rc = fs.run_with_failure_limit(tmp_path, 5, run, state_dir=tmp_path / "s")

        assert rc == SENTINEL_EXIT_CODE
        assert capsys.readouterr().out == "hello\n"

    def test_unwritable_state_does_not_change_the_decision(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Bookkeeping failures fail open: the block is still reported."""
        blocker = tmp_path / "state"
        blocker.write_text("a file, not a directory", encoding="utf-8")

        rc = fs.run_with_failure_limit(tmp_path, 1, _blocking_run, state_dir=blocker)

        assert rc == 0
        assert json.loads(capsys.readouterr().out)["decision"] == "block"

    def test_an_unclearable_streak_runs_the_checks_instead_of_skipping(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without a successful reset, a tripped breaker would skip every stop.

        Deleting the streak file is what lets checks resume, so when it fails
        the breaker must not short-circuit: it runs the checks as usual.
        """
        directory = tmp_path / "work"
        root = tmp_path / "state"
        path = fs.streak_path(directory, root)
        fs.write_streak(path, directory, LIMIT)
        run = _CountingRun(_blocking_run)

        def refuse_unlink(self: Path, *, missing_ok: bool = False) -> None:
            """Fail like a read-only state directory would."""
            message = f"read-only state directory: {self}"
            raise PermissionError(message)

        monkeypatch.setattr(Path, "unlink", refuse_unlink)
        for _ in range(SMALL_LIMIT):
            fs.run_with_failure_limit(directory, LIMIT, run, state_dir=root)

        assert run.calls == SMALL_LIMIT, "an unclearable streak must not skip checks"
        outputs = capsys.readouterr().out.splitlines()
        assert all(json.loads(line)["decision"] == "block" for line in outputs)

    def test_output_is_forwarded_when_the_checks_raise(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Captured output is not lost when the wrapped run fails."""

        def run() -> int:
            """Print a line and then fail unexpectedly."""
            print("partial")
            message = "boom"
            raise RuntimeError(message)

        with pytest.raises(RuntimeError, match="boom"):
            fs.run_with_failure_limit(tmp_path, 1, run, state_dir=tmp_path / "s")

        assert capsys.readouterr().out == "partial\n"


class TestStreakFile:
    """Read and write the per-directory streak file."""

    def test_missing_file_reads_as_zero(self, tmp_path: Path) -> None:
        """No file means no recorded failures."""
        assert fs.read_streak(tmp_path / "absent.json") == 0

    @pytest.mark.parametrize(
        "contents",
        [
            "not json",
            "[]",
            '{"consecutive_failures": "3"}',
            '{"consecutive_failures": true}',
            '{"consecutive_failures": -1}',
        ],
    )
    def test_corrupt_file_reads_as_zero(self, tmp_path: Path, contents: str) -> None:
        """Unreadable state is treated as a fresh streak."""
        path = tmp_path / "streak.json"
        path.write_text(contents, encoding="utf-8")
        assert fs.read_streak(path) == 0

    def test_round_trip(self, tmp_path: Path) -> None:
        """A written count reads back and records its directory."""
        path = fs.streak_path(tmp_path / "work", tmp_path / "state")
        fs.write_streak(path, tmp_path / "work", STORED_COUNT)

        assert fs.read_streak(path) == STORED_COUNT
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["directory"] == str(tmp_path / "work")

    def test_failed_rename_removes_the_temporary_file(self, tmp_path: Path) -> None:
        """A rename onto a directory fails without leaving temporary files."""
        root = tmp_path / "state"
        path = fs.streak_path(tmp_path / "work", root)
        path.mkdir(parents=True)
        (path / "occupant").touch()

        with pytest.raises(OSError, match=r".+"):
            fs.write_streak(path, tmp_path / "work", 1)

        assert sorted(p.name for p in root.iterdir()) == [path.name]

    def test_streak_path_is_stable_per_directory(self, tmp_path: Path) -> None:
        """The same directory maps to the same file; others do not collide."""
        root = tmp_path / "state"
        first = fs.streak_path(tmp_path / "a", root)
        assert first == fs.streak_path(tmp_path / "a", root)
        assert first != fs.streak_path(tmp_path / "b", root)
        assert first.parent == root
