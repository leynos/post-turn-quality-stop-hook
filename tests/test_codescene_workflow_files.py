"""Prove the CV-005 file readers: what they read, key and refuse.

The readers are the only part of the contract that touches the disk. A reader
that silently skips a file or directory would let every rule built on it pass
over an empty subject set, so each refusal here is a clause of the contract.
"""

from __future__ import annotations

import pathlib
import typing as typ

import pytest
from codescene_workflow_files import read_actions, read_workflows
from codescene_workflow_reader import WorkflowError, load_workflow

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


def test_reader_reads_every_local_action(tmp_path: Path) -> None:
    """Actions are keyed by directory, at any depth and outside `.github` too."""
    for directory in (".github/actions/a", ".github/actions/deep/b", "tools/c"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / ".github/actions/a/action.yml").write_text("runs: {}\n")
    (tmp_path / ".github/actions/deep/b/action.yaml").write_text("runs: {}\n")
    (tmp_path / "tools/c/action.yml").write_text("runs: {}\n")
    found = sorted(read_actions(tmp_path))
    expected = [".github/actions/a", ".github/actions/deep/b", "tools/c"]
    assert found == expected, f"actions read: {found}"


def test_reader_skips_hidden_and_vendored_directories(tmp_path: Path) -> None:
    """A virtual environment or dependency tree holds no action to follow."""
    skipped = (".venv/lib/x", "__pycache__/w", "node_modules/y", "target/z")
    for directory in (*skipped, "tools/ok"):
        (tmp_path / directory).mkdir(parents=True)
        (tmp_path / directory / "action.yml").write_text("runs: {}\n")
    found = sorted(read_actions(tmp_path))
    assert found == ["tools/ok"], f"actions read: {found}"


def test_reader_refuses_a_directory_it_cannot_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable directory could hide an action, so it is not skipped.

    The walk error is injected rather than made with permission bits, which a
    privileged process or another platform would ignore.
    """

    def failing_walk(
        self: pathlib.Path,
        *_: object,
        on_error: cabc.Callable[[OSError], object] | None = None,
        **__: object,
    ) -> cabc.Iterator[tuple[pathlib.Path, list[str], list[str]]]:
        if on_error is not None:
            on_error(PermissionError(13, "Permission denied", str(self / "tools")))
        yield from ()

    monkeypatch.setattr(pathlib.Path, "walk", failing_walk)
    with pytest.raises(
        WorkflowError, match=r"cannot search .* for local actions: .*tools"
    ):
        read_actions(tmp_path)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"\xff\xfe", r"^action\.yml: cannot be read as UTF-8"),
        (b"runs: [\n", r"^\.github/actions/bad/action\.yml: not valid YAML"),
    ],
)
def test_reader_names_an_unreadable_action(
    tmp_path: Path, content: bytes, reason: str
) -> None:
    """A broken action fails at the reader, naming its file."""
    directory = tmp_path / ".github/actions/bad"
    directory.mkdir(parents=True)
    (directory / "action.yml").write_bytes(content)
    with pytest.raises(WorkflowError, match=reason):
        read_actions(tmp_path)


def test_reader_refuses_an_action_declared_twice(tmp_path: Path) -> None:
    """GitHub reads one metadata file; a reader of either could be misled."""
    directory = tmp_path / ".github/actions/a"
    directory.mkdir(parents=True)
    for name in ("action.yml", "action.yaml"):
        (directory / name).write_text("runs: {}\n")
    with pytest.raises(WorkflowError, match="declares both"):
        read_actions(tmp_path)


def test_reader_refuses_an_empty_directory(tmp_path: Path) -> None:
    """Finding no workflow is the reader failing, not the repository passing."""
    with pytest.raises(WorkflowError, match="no workflows were read"):
        read_workflows(tmp_path)


def test_reader_reads_every_suffix_and_case(tmp_path: Path) -> None:
    """GitHub runs `.yaml` and upper-case suffixes too."""
    for name in ("a.YML", "b.yaml"):
        (tmp_path / name).write_text("on: push\njobs: {}\n", encoding="utf-8")
    found = sorted(read_workflows(tmp_path))
    assert found == ["a.YML", "b.yaml"], f"workflows read: {found}"


def test_reader_refuses_a_missing_directory(tmp_path: Path) -> None:
    """A directory that cannot be listed fails at the reader, naming it."""
    with pytest.raises(WorkflowError, match="cannot list workflows"):
        read_workflows(tmp_path / "missing")


def test_reader_refuses_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    """Undecodable bytes fail at the reader, naming the file."""
    (tmp_path / "bad.yml").write_bytes(b"\xff\xfe")
    with pytest.raises(WorkflowError, match=r"^bad\.yml: cannot be read as UTF-8"):
        read_workflows(tmp_path)


def test_reader_names_the_file_for_invalid_yaml() -> None:
    """A parser error must say which workflow it came from."""
    with pytest.raises(WorkflowError, match=r"^bad\.yml: not valid YAML"):
        load_workflow("bad.yml", "jobs: [\n")
