"""Read workflow and local action files from disk, for the CV-005 contract.

This is the only part of the contract that touches the disk; the parsing and
every rule are pure, in `codescene_workflow_reader` and the rule modules.
"""

from __future__ import annotations

import typing as typ

from codescene_workflow_reader import Document, WorkflowError, load_workflow

if typ.TYPE_CHECKING:
    from pathlib import Path

#: Directories never searched for local actions. Hidden directories other than
#: `.github` (version control, virtual environments, tool caches) are skipped
#: as well; none holds an action a workflow in this repository can name.
UNSEARCHED: typ.Final[frozenset[str]] = frozenset({
    "__pycache__",
    "node_modules",
    "target",
})
ACTION_FILES: typ.Final[tuple[str, ...]] = ("action.yml", "action.yaml")


def read_workflows(directory: Path) -> dict[str, Document]:
    """Parse every workflow in a directory, by file name.

    Both suffixes and any case are read, because GitHub runs all of them.

    Parameters
    ----------
    directory : Path
        The workflow directory.

    Returns
    -------
    dict of str to Document
        Each workflow's parsed document, keyed by file name.

    Raises
    ------
    WorkflowError
        If the directory cannot be listed or holds no workflow, or any
        workflow cannot be read as UTF-8 or fails to parse.

    """
    try:
        paths = sorted(
            path
            for path in directory.iterdir()
            if path.suffix.casefold() in {".yml", ".yaml"}
        )
    except OSError as error:
        message = f"cannot list workflows in {directory}: {error}"
        raise WorkflowError(message) from error
    if not paths:
        message = f"no workflows were read from {directory}"
        raise WorkflowError(message)
    return {path.name: load_workflow(path.name, _read_text(path)) for path in paths}


def read_actions(root: Path) -> dict[str, Document]:
    """Parse every local action in the repository, keyed by its directory.

    A step's `./` or `$/` reference names the directory holding the action's
    metadata, anywhere in the repository, so that directory, relative to the
    root, is the key. No local action is a valid repository, so an empty result
    is not a fault; a directory the search cannot read is, because an action
    inside it would otherwise go unread.

    Parameters
    ----------
    root : Path
        The repository root.

    Returns
    -------
    dict of str to Document
        Each action's parsed metadata, keyed by its directory, such as
        `.github/actions/build-wheels`.

    Raises
    ------
    WorkflowError
        If a directory cannot be searched, a directory holds both `action.yml`
        and `action.yaml`, or any action cannot be read as UTF-8 or fails to
        parse.

    """
    paths = sorted(_action_files(root))
    actions: dict[str, Document] = {}
    for path in paths:
        key = path.parent.relative_to(root).as_posix()
        if key in actions:
            message = f"{key}: declares both action.yml and action.yaml"
            raise WorkflowError(message)
        actions[key] = load_workflow(f"{key}/{path.name}", _read_text(path))
    return actions


def _searched(name: str) -> bool:
    """Return whether the action search descends into a directory."""
    return name not in UNSEARCHED and (name == ".github" or not name.startswith("."))


def _action_files(root: Path) -> list[Path]:
    """Return every action metadata file in the repository.

    `Path.walk` ignores an unreadable directory unless told otherwise, which
    would read an action behind it as absent, so a walk error is refused.
    """

    def refuse(error: OSError) -> None:
        message = f"cannot search {root} for local actions: {error}"
        raise WorkflowError(message) from error

    found: list[Path] = []
    for directory, children, files in root.walk(on_error=refuse):
        children[:] = [name for name in children if _searched(name)]
        found += [directory / name for name in ACTION_FILES if name in files]
    return found


def _read_text(path: Path) -> str:
    """Read one workflow as UTF-8, naming the file on failure."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        message = f"{path.name}: cannot be read as UTF-8 text: {error}"
        raise WorkflowError(message) from error
