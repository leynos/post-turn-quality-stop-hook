"""Read GitHub workflow files strictly, for the CV-005 contract.

Only `read_workflows` and `read_actions` touch the disk; everything else is
pure over parsed documents, so the rules in `codescene_pull_request_rules` and
`codescene_publisher_rules` can be driven over mutated copies as readily as
over this repository's files.

A reading that finds nothing is a fault of the reader, not a pass: every rule
built on these readings is a refusal, and a refusal over an empty subject set
is satisfied by any repository at all. Those faults raise `WorkflowError`.
"""

from __future__ import annotations

import re
import typing as typ

import yaml

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

type Document = dict[object, object]
type Step = dict[str, object]


class WorkflowError(ValueError):
    """Raised when the workflows cannot be read as the rules require."""


class _StrictLoader(yaml.SafeLoader):
    """A safe loader that refuses a key declared twice in one mapping.

    PyYAML keeps the last duplicate silently, so a lane declaring `runs-on`
    or `if` twice would be judged on the half GitHub may not use.
    """


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode) -> Document:
    """Build a mapping, raising on a repeated key."""
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            message = f"duplicate key {key!r} at {key_node.start_mark}"
            raise WorkflowError(message)
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_StrictLoader.add_constructor(_StrictLoader.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_workflow(name: str, text: str) -> Document:
    r"""Parse one workflow strictly, naming the file on failure.

    Parameters
    ----------
    name : str
        The workflow's file name, for messages.
    text : str
        The workflow's YAML source.

    Returns
    -------
    Document
        The parsed workflow mapping.

    Raises
    ------
    WorkflowError
        If the text is not valid YAML, repeats a key within one mapping, or is
        not a mapping.

    Examples
    --------
    >>> load_workflow("ci.yml", "on: push\njobs: {}\n")
    {True: 'push', 'jobs': {}}

    """
    loader = _StrictLoader(text)
    try:
        document = loader.get_single_data()
    except yaml.YAMLError as error:
        message = f"{name}: not valid YAML: {error}"
        raise WorkflowError(message) from error
    except WorkflowError as error:
        message = f"{name}: {error}"
        raise WorkflowError(message) from error
    finally:
        loader.dispose()
    if not isinstance(document, dict):
        message = f"{name}: a workflow must be a mapping"
        raise WorkflowError(message)
    return document


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
    """Parse every local action under `.github`, keyed by its directory.

    A step's `./` or `$/` reference names the directory holding the action's
    metadata, so that directory, relative to the repository root, is the key.
    No local action is a valid repository, so an empty result is not a fault.

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
        If a directory holds both `action.yml` and `action.yaml`, or any
        action cannot be read as UTF-8 or fails to parse.

    """
    paths = sorted(
        path
        for name in ("action.yml", "action.yaml")
        for path in (root / ".github").rglob(name)
    )
    actions: dict[str, Document] = {}
    for path in paths:
        key = path.parent.relative_to(root).as_posix()
        if key in actions:
            message = f"{key}: declares both action.yml and action.yaml"
            raise WorkflowError(message)
        actions[key] = load_workflow(f"{key}/{path.name}", _read_text(path))
    return actions


def _read_text(path: Path) -> str:
    """Read one workflow as UTF-8, naming the file on failure."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        message = f"{path.name}: cannot be read as UTF-8 text: {error}"
        raise WorkflowError(message) from error


def triggers(name: str, document: Document) -> dict[str, object]:
    """Return a workflow's events in mapping form.

    YAML 1.1 reads a bare `on` as boolean true, and a quoted `'on'` as the
    string. GitHub merges the two, so a workflow declaring both is refused
    rather than read by half.

    Parameters
    ----------
    name : str
        The workflow's file name, for messages.
    document : Document
        The parsed workflow.

    Returns
    -------
    dict of str to object
        Each event name mapped to its filter, or None where the scalar or
        sequence form gives none.

    Raises
    ------
    WorkflowError
        If `on` is declared under neither or both spellings, or has a shape
        other than a scalar, a sequence of names or a mapping.

    """
    spellings = [key for key in ("on", True) if key in document]
    if len(spellings) != 1:
        message = f"{name}: declares `on` {len(spellings)} times, not once"
        raise WorkflowError(message)
    match document[spellings[0]]:
        case str() as event:
            return {event: None}
        case list() as events if all(isinstance(event, str) for event in events):
            return dict.fromkeys(typ.cast("list[str]", events))
        case dict() as events:
            return {str(event): value for event, value in events.items()}
        case other:
            message = f"{name}: cannot read the trigger {other!r}"
            raise WorkflowError(message)


def jobs(name: str, document: Document) -> dict[str, dict[str, object]]:
    """Return a workflow's jobs, refusing a malformed `jobs` block.

    Parameters
    ----------
    name : str
        The workflow's file name, for messages.
    document : Document
        The parsed workflow.

    Returns
    -------
    dict of str to dict
        Each job's mapping, keyed by job id.

    Raises
    ------
    WorkflowError
        If `jobs` is missing or does not map job ids to mappings.

    """
    found = document.get("jobs")
    if not isinstance(found, dict) or not all(
        isinstance(job, dict) for job in found.values()
    ):
        message = f"{name}: `jobs` must map job names to mappings"
        raise WorkflowError(message)
    return typ.cast("dict[str, dict[str, object]]", found)


def steps(name: str, document: Document) -> cabc.Iterator[Step]:
    """Yield every step of every job in one workflow.

    Parameters
    ----------
    name : str
        The workflow's file name, for messages.
    document : Document
        The parsed workflow.

    Yields
    ------
    Step
        Each step mapping, in job and step order.

    Raises
    ------
    WorkflowError
        If the jobs are malformed or a step is not a mapping.

    """
    for job in jobs(name, document).values():
        for step in typ.cast("list[object]", job.get("steps", [])):
            if not isinstance(step, dict):
                message = f"{name}: a step must be a mapping"
                raise WorkflowError(message)
            yield typ.cast("Step", step)


def calls(step: Step, action: str) -> bool:
    """Return whether a step calls one shared action, at any ref.

    Parameters
    ----------
    step : Step
        The step mapping.
    action : str
        The action path, without a ref.

    Returns
    -------
    bool
        True when the step's `uses:` names the action, compared without case.

    """
    uses = str(step.get("uses", ""))
    return uses.partition("@")[0].casefold() == action.casefold()


def scalars(value: object) -> cabc.Iterator[str]:
    """Yield every key and value in a parsed document as text.

    Keys are yielded as well as values: an `env` key or a `workflow_call`
    secret declaration names the token with no value that refers to it.

    Parameters
    ----------
    value : object
        A parsed document or any part of one.

    Yields
    ------
    str
        Each key and each non-null leaf value, as text.

    """
    match value:
        case dict():
            for key, child in value.items():
                yield str(key)
                yield from scalars(child)
        case list():
            for child in value:
                yield from scalars(child)
        case None:
            return
        case _:
            yield str(value)


def folded(text: str) -> str:
    """Return text case-folded with all whitespace removed.

    Parameters
    ----------
    text : str
        The text to normalize.

    Returns
    -------
    str
        The text, so that `toJSON( secrets )` and `API.CODESCENE.IO` match
        their plain spellings.

    """
    return re.sub(r"\s+", "", text).casefold()


def continues_on_error(mapping: dict[str, object]) -> bool:
    """Return whether a step or job may fail without failing its run.

    `continue-on-error` keeps the step running while its failure turns green,
    which silences a ratchet or an upload as surely as `if: false` does.
    """
    return mapping.get("continue-on-error", False) is not False


def holding_job(name: str, document: Document, step: Step) -> dict[str, object]:
    """Return the job in one workflow whose steps include this step."""
    return next(
        job
        for job in jobs(name, document).values()
        if any(held is step for held in typ.cast("list[object]", job.get("steps", [])))
    )
