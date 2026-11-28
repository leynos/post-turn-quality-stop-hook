"""Unit tests for the file-system ``StreakStore`` adapter."""

from __future__ import annotations

import concurrent.futures
import errno
import hashlib
import json
import logging
import os
import threading
import typing as typ
from pathlib import Path

import pytest

from post_turn_quality_stop_hook import failure_breaker as fb
from post_turn_quality_stop_hook import streak_store as ss
from post_turn_quality_stop_hook.failure_breaker import (
    CheckOutcome,
    CheckResult,
    StreakId,
    StreakStoreError,
)

STORED_COUNT = 4
LIMIT = 3
WORKERS = 8
STREAK = StreakId("0" * 64)


class _WriteFailure(typ.NamedTuple):
    """Path methods to refuse, and the store operation that should fail."""

    refused: tuple[str, ...]
    operation: str


def _no_home() -> Path:
    """Fail as ``Path.home()`` does when no home directory is known."""
    message = "Could not determine home directory."
    raise RuntimeError(message)


def _refuse(path: Path, *_args: object, **_kwargs: object) -> typ.NoReturn:
    """Fail like an unreadable or read-only state directory."""
    raise PermissionError(errno.EACCES, "Permission denied", str(path))


class TestDirectoryStreakId:
    """Directory identities are stable, opaque hashes."""

    def test_identity_is_the_sha256_of_the_encoded_path(self) -> None:
        """Existing state files keep their names across the refactor."""
        directory = Path("/work/repo")
        expected = hashlib.sha256(os.fsencode(directory)).hexdigest()
        assert ss.directory_streak_id(directory) == expected

    def test_identity_is_stable_and_distinct(self) -> None:
        """The same directory maps to one identity; others do not collide."""
        first = ss.directory_streak_id(Path("/work/a"))
        assert first == ss.directory_streak_id(Path("/work/a"))
        assert first != ss.directory_streak_id(Path("/work/b"))

    def test_undecodable_directory_names_hash(self) -> None:
        """Surrogate-escaped bytes from non-UTF-8 names hash without error."""
        directory = Path(os.fsdecode(b"/work/caf\xe9"))
        expected = hashlib.sha256(b"/work/caf\xe9").hexdigest()
        assert ss.directory_streak_id(directory) == expected


class TestStateRoot:
    """Resolve the XDG state directory from explicit inputs."""

    def test_uses_absolute_xdg_state_home(self, tmp_path: Path) -> None:
        """An absolute XDG_STATE_HOME is honoured."""
        root = ss.state_root({"XDG_STATE_HOME": str(tmp_path)}, _no_home)
        assert root == tmp_path / "post-turn-quality-stop-hook" / "failure-streaks"

    def test_an_unresolvable_home_is_reported_for_the_fallback(self) -> None:
        """Only the fallback asks for the home directory, which may fail."""
        with pytest.raises(RuntimeError, match="home"):
            ss.state_root({}, _no_home)

    @pytest.mark.parametrize(
        "environ",
        [{}, {"XDG_STATE_HOME": ""}, {"XDG_STATE_HOME": "relative/state"}],
    )
    def test_ignores_missing_empty_or_relative_xdg_state_home(
        self, environ: dict[str, str], tmp_path: Path
    ) -> None:
        """The XDG spec says empty and relative values must be ignored."""
        root = ss.state_root(environ, lambda: tmp_path)
        expected = (
            tmp_path / ".local" / "state" / "post-turn-quality-stop-hook"
        ) / "failure-streaks"
        assert root == expected


class TestStreakFile:
    """Read and write one streak file."""

    def test_missing_file_reads_as_zero(self, tmp_path: Path) -> None:
        """No file means no recorded failures."""
        assert ss.read_streak(tmp_path / "absent.json") == 0

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
    def test_corrupt_file_is_an_error(self, tmp_path: Path, contents: str) -> None:
        """Invalid state is reported; the policy decides what it means."""
        path = tmp_path / "streak.json"
        path.write_text(contents, encoding="utf-8")
        with pytest.raises(ValueError, match=r".+"):
            ss.read_streak(path)

    def test_round_trip(self, tmp_path: Path) -> None:
        """A written count reads back."""
        path = tmp_path / "state" / "streak.json"
        ss.write_streak(path, STORED_COUNT)

        assert ss.read_streak(path) == STORED_COUNT
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "consecutive_failures": STORED_COUNT
        }

    def test_records_from_earlier_versions_still_load(self, tmp_path: Path) -> None:
        """Files that also record the directory keep their count."""
        path = tmp_path / "streak.json"
        payload = {"directory": "/work", "consecutive_failures": STORED_COUNT}
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert ss.read_streak(path) == STORED_COUNT

    def test_failed_rename_removes_the_temporary_file(self, tmp_path: Path) -> None:
        """A rename onto a directory fails without leaving temporary files."""
        root = tmp_path / "state"
        path = root / "streak.json"
        path.mkdir(parents=True)
        (path / "occupant").touch()

        with pytest.raises(OSError, match=r".+"):
            ss.write_streak(path, 1)

        assert sorted(p.name for p in root.iterdir()) == [path.name]


class TestFileStreakStore:
    """The adapter implements the port's atomic operations."""

    def test_increment_load_and_clear(self, tmp_path: Path) -> None:
        """Increments accumulate and load back until cleared."""
        store = ss.FileStreakStore(tmp_path / "state")

        assert store.load(STREAK) == 0
        assert [store.increment(STREAK) for _ in range(2)] == [1, 2]
        assert store.load(STREAK) == 2  # noqa: PLR2004 - two increments
        store.clear(STREAK)
        assert store.load(STREAK) == 0
        store.clear(STREAK)  # clearing an absent streak is not an error

    def test_increment_replaces_a_corrupt_record(self, tmp_path: Path) -> None:
        """A corrupt file restarts the count rather than blocking it forever."""
        store = ss.FileStreakStore(tmp_path / "state")
        store.root.mkdir(parents=True)
        store.path_for(STREAK).write_text("not json", encoding="utf-8")

        assert store.increment(STREAK) == 1

    @pytest.mark.parametrize(
        ("stored", "cleared"), [(LIMIT - 1, 0), (LIMIT, LIMIT), (LIMIT + 1, LIMIT + 1)]
    )
    def test_reset_if_tripped(self, tmp_path: Path, stored: int, cleared: int) -> None:
        """Only a count that reaches the limit is reset, and it is returned."""
        store = ss.FileStreakStore(tmp_path / "state")
        ss.write_streak(store.path_for(STREAK), stored)

        assert store.reset_if_tripped(STREAK, LIMIT) == cleared
        assert store.load(STREAK) == (0 if cleared else stored)

    @pytest.mark.parametrize("operation", ["clear", "reset"])
    def test_an_undeletable_record_is_zeroed_in_place(
        self, tmp_path: Path, operation: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pass or reset is still recorded when the file cannot be deleted."""
        store = ss.FileStreakStore(tmp_path / "state")
        ss.write_streak(store.path_for(STREAK), LIMIT)
        monkeypatch.setattr(Path, "unlink", _refuse)

        if operation == "clear":
            store.clear(STREAK)
        else:
            assert store.reset_if_tripped(STREAK, LIMIT) == LIMIT

        assert store.load(STREAK) == 0

    def test_only_one_concurrent_reset_claims_a_tripped_streak(
        self, tmp_path: Path
    ) -> None:
        """Simultaneous trips in one directory short-circuit exactly once."""
        store = ss.FileStreakStore(tmp_path / "state")
        ss.write_streak(store.path_for(STREAK), LIMIT)
        barrier = threading.Barrier(WORKERS)

        def claim() -> int:
            """Contend for the reset with every other worker at once."""
            barrier.wait()
            return ss.FileStreakStore(store.root).reset_if_tripped(STREAK, LIMIT)

        with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
            claims = list(pool.map(lambda _: claim(), range(WORKERS)))

        assert sorted(claims) == [0] * (WORKERS - 1) + [LIMIT]

    def test_concurrent_increments_are_not_lost(self, tmp_path: Path) -> None:
        """Overlapping blocks each add one to the count."""
        store = ss.FileStreakStore(tmp_path / "state")
        barrier = threading.Barrier(WORKERS)

        def block() -> int:
            """Record one block alongside every other worker."""
            barrier.wait()
            return ss.FileStreakStore(store.root).increment(STREAK)

        with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
            counts = sorted(pool.map(lambda _: block(), range(WORKERS)))

        assert counts == list(range(1, WORKERS + 1))
        assert store.load(STREAK) == WORKERS

    def test_a_pass_that_lands_during_a_block_is_not_overwritten(
        self, tmp_path: Path
    ) -> None:
        """A block that finishes after a pass counts from the pass, not before.

        Run A loads a streak of ``LIMIT - 1`` and starts its checks. Run B
        passes and clears the streak. A then blocks: the stored count must be
        one, not ``LIMIT``, which would trip the breaker on stale history.
        """
        store = ss.FileStreakStore(tmp_path / "state")
        ss.write_streak(store.path_for(STREAK), LIMIT - 1)

        def run_a() -> CheckResult:
            """Let run B pass and clear the streak while A's checks run."""
            result_b = fb.run_with_failure_limit(
                STREAK,
                LIMIT,
                lambda: CheckResult(exit_code=0, outcome=CheckOutcome.PASSED),
                store,
            )
            assert result_b == CheckResult(exit_code=0, outcome=CheckOutcome.PASSED)
            return CheckResult(exit_code=0, outcome=CheckOutcome.BLOCKED)

        fb.run_with_failure_limit(STREAK, LIMIT, run_a, store)

        assert store.load(STREAK) == 1


class TestStoreErrors:
    """Storage failures surface as path-free ``StreakStoreError``s."""

    @staticmethod
    def _store_inside(tmp_path: Path) -> tuple[Path, ss.FileStreakStore]:
        """Return a working directory whose XDG state lives inside it."""
        workdir = tmp_path / "private-project"
        root = ss.state_root({"XDG_STATE_HOME": str(workdir / "state")}, _no_home)
        return workdir, ss.FileStreakStore(root)

    def test_corrupt_state_is_a_load_error(self, tmp_path: Path) -> None:
        """Corrupt files surface through the port's error type."""
        workdir, store = self._store_inside(tmp_path)
        store.root.mkdir(parents=True)
        store.path_for(STREAK).write_text("not json", encoding="utf-8")

        with pytest.raises(StreakStoreError) as caught:
            store.load(STREAK)

        assert str(caught.value) == "cannot load streak: invalid streak record"
        assert str(workdir) not in str(caught.value)

    @pytest.mark.parametrize("operation", ["load", "increment", "clear", "reset"])
    def test_failures_name_the_operation_but_not_the_path(
        self,
        tmp_path: Path,
        operation: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Warnings stay free of the state path even under the working tree.

        With ``XDG_STATE_HOME`` inside the working directory, a path in the
        error text would disclose that directory despite the hashed identity.
        """
        workdir, store = self._store_inside(tmp_path)
        store.root.mkdir(parents=True)
        ss.write_streak(store.path_for(STREAK), LIMIT)

        for method in ("read_text", "write_text", "unlink"):
            monkeypatch.setattr(Path, method, _refuse)

        attempts = {
            "load": lambda: store.load(STREAK),
            "increment": lambda: store.increment(STREAK),
            "clear": lambda: store.clear(STREAK),
            "reset": lambda: store.reset_if_tripped(STREAK, LIMIT),
        }
        with pytest.raises(StreakStoreError) as caught:
            attempts[operation]()

        assert (
            str(caught.value) == f"cannot {operation} streak: PermissionError (EACCES)"
        )
        assert str(workdir) not in str(caught.value)

    def test_breaker_warnings_do_not_disclose_the_path(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """End to end, a failing file store logs no raw path."""
        workdir = tmp_path / "private-project"
        blocker = workdir / "state"
        blocker.parent.mkdir()
        blocker.write_text("a file, not a directory", encoding="utf-8")
        store = ss.FileStreakStore(blocker / "streaks")

        with caplog.at_level(logging.WARNING, logger=fb.__name__):
            fb.run_with_failure_limit(
                ss.directory_streak_id(workdir),
                LIMIT,
                lambda: CheckResult(exit_code=0, outcome=CheckOutcome.BLOCKED),
                store,
            )

        messages = [record.getMessage() for record in caplog.records]
        assert [m.split()[3] for m in messages] == [
            "operation=load",
            "operation=increment",
        ]
        assert not any(str(workdir) in m for m in messages)

    @pytest.mark.parametrize(
        "case",
        [
            _WriteFailure(("replace",), "increment"),
            _WriteFailure(("unlink", "write_text"), "reset"),
        ],
        ids=["increment", "reset"],
    )
    def test_write_failures_fail_open_without_disclosing_the_path(
        self,
        tmp_path: Path,
        case: _WriteFailure,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failed increment writes and reset deletions run the checks.

        Reads succeed, so each case fails at the write itself: the atomic
        rename for an increment, and both the delete and the in-place
        overwrite for a reset. The block still reaches the agent, and the
        warning names the operation without the working directory.
        """
        workdir, store = self._store_inside(tmp_path)
        operation = case.operation
        stored = LIMIT if operation == "reset" else 0
        ss.write_streak(store.path_for(STREAK), stored)
        for method in case.refused:
            monkeypatch.setattr(Path, method, _refuse)
        blocked = CheckResult(exit_code=0, outcome=CheckOutcome.BLOCKED)

        with caplog.at_level(logging.WARNING, logger=fb.__name__):
            result = fb.run_with_failure_limit(STREAK, LIMIT, lambda: blocked, store)

        assert result == blocked
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            f"operation={operation} " in m and "PermissionError (EACCES)" in m
            for m in messages
        )
        assert not any(str(workdir) in m for m in messages)

    def test_unwritable_root_is_a_store_error(self, tmp_path: Path) -> None:
        """A root under a file, not a directory, cannot hold streaks."""
        blocker = tmp_path / "state"
        blocker.write_text("a file, not a directory", encoding="utf-8")
        store = ss.FileStreakStore(blocker / "streaks")

        with pytest.raises(StreakStoreError, match="cannot increment streak"):
            store.increment(STREAK)
