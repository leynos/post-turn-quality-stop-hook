"""Unit tests for the file-system ``StreakStore`` adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from post_turn_quality_stop_hook import streak_store as ss
from post_turn_quality_stop_hook.failure_breaker import StreakStoreError

STORED_COUNT = 4


class TestStateRoot:
    """Resolve the XDG state directory from explicit inputs."""

    def test_uses_absolute_xdg_state_home(self, tmp_path: Path) -> None:
        """An absolute XDG_STATE_HOME is honoured."""
        root = ss.state_root({"XDG_STATE_HOME": str(tmp_path)}, tmp_path / "home")
        assert root == tmp_path / "post-turn-quality-stop-hook" / "failure-streaks"

    @pytest.mark.parametrize(
        "environ", [{}, {"XDG_STATE_HOME": ""}, {"XDG_STATE_HOME": "relative/state"}]
    )
    def test_ignores_missing_empty_or_relative_xdg_state_home(
        self, environ: dict[str, str], tmp_path: Path
    ) -> None:
        """The XDG spec says empty and relative values must be ignored."""
        root = ss.state_root(environ, tmp_path)
        expected = (
            tmp_path / ".local" / "state" / "post-turn-quality-stop-hook"
        ) / "failure-streaks"
        assert root == expected


class TestStreakFile:
    """Read and write the per-directory streak file."""

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
        """A written count reads back and records its directory."""
        path = ss.streak_path(tmp_path / "work", tmp_path / "state")
        ss.write_streak(path, tmp_path / "work", STORED_COUNT)

        assert ss.read_streak(path) == STORED_COUNT
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["directory"] == str(tmp_path / "work")

    def test_failed_rename_removes_the_temporary_file(self, tmp_path: Path) -> None:
        """A rename onto a directory fails without leaving temporary files."""
        root = tmp_path / "state"
        path = ss.streak_path(tmp_path / "work", root)
        path.mkdir(parents=True)
        (path / "occupant").touch()

        with pytest.raises(OSError, match=r".+"):
            ss.write_streak(path, tmp_path / "work", 1)

        assert sorted(p.name for p in root.iterdir()) == [path.name]

    def test_streak_path_is_stable_per_directory(self, tmp_path: Path) -> None:
        """The same directory maps to the same file; others do not collide."""
        root = tmp_path / "state"
        first = ss.streak_path(tmp_path / "a", root)
        assert first == ss.streak_path(tmp_path / "a", root)
        assert first != ss.streak_path(tmp_path / "b", root)
        assert first.parent == root


class TestFileStreakStore:
    """The adapter implements the port and reports storage failures."""

    def test_save_load_and_clear(self, tmp_path: Path) -> None:
        """A saved count loads back until it is cleared."""
        store = ss.FileStreakStore(tmp_path / "state")
        directory = tmp_path / "work"

        assert store.load(directory) == 0
        store.save(directory, STORED_COUNT)
        assert store.load(directory) == STORED_COUNT
        store.clear(directory)
        assert store.load(directory) == 0
        store.clear(directory)  # clearing an absent streak is not an error

    def test_undecodable_directory_names_round_trip(self, tmp_path: Path) -> None:
        """A non-UTF-8 byte in the path must not break the store.

        Python surrogate-escapes such bytes, which strict UTF-8 encoding
        rejects; the filesystem codec round-trips them.
        """
        store = ss.FileStreakStore(tmp_path / "state")
        directory = Path(os.fsdecode(os.fsencode(tmp_path) + b"/caf\xe9"))

        store.save(directory, STORED_COUNT)

        assert store.load(directory) == STORED_COUNT

    def test_corrupt_state_is_a_store_error(self, tmp_path: Path) -> None:
        """Corrupt files surface through the port's error type."""
        store = ss.FileStreakStore(tmp_path / "state")
        path = ss.streak_path(tmp_path / "work", store.root)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")

        with pytest.raises(StreakStoreError, match="cannot read"):
            store.load(tmp_path / "work")

    def test_unwritable_root_is_a_store_error(self, tmp_path: Path) -> None:
        """A root that is a file, not a directory, cannot hold streaks."""
        blocker = tmp_path / "state"
        blocker.write_text("a file, not a directory", encoding="utf-8")
        store = ss.FileStreakStore(blocker)

        with pytest.raises(StreakStoreError, match="cannot write"):
            store.save(tmp_path / "work", 1)

    def test_unclearable_streak_is_a_store_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed delete surfaces so the policy can refuse to trip."""
        store = ss.FileStreakStore(tmp_path / "state")

        def refuse_unlink(self: Path, *, missing_ok: bool = False) -> None:
            """Fail like a read-only state directory would."""
            message = f"read-only state directory: {self}"
            raise PermissionError(message)

        monkeypatch.setattr(Path, "unlink", refuse_unlink)
        with pytest.raises(StreakStoreError, match="cannot clear"):
            store.clear(tmp_path / "work")
