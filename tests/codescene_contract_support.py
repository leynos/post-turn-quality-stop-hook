"""Shared inputs for the CV-005 contract tests.

The contract tests mutate a copy of this repository's workflows in the way a
later edit could, and assert that the clause meant to catch that edit does.
These helpers hand each test its own copy and find the parts they mutate.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

from codescene_publisher_rules import upload_steps
from codescene_workflow_files import read_actions, read_workflows
from codescene_workflow_reader import Document, Step

if typ.TYPE_CHECKING:
    import collections.abc as cabc

type Documents = dict[str, Document]
type Rule = cabc.Callable[[Documents], list[str]]

ROOT: typ.Final[Path] = Path(__file__).resolve().parents[1]
WORKFLOWS: typ.Final[Path] = ROOT / ".github" / "workflows"
#: The pull-request lane, which the mutation cases extend.
LANE: typ.Final[str] = "ci.yml"
PROBE: typ.Final[str] = "probe.yml"
CREDENTIAL_REFERENCE: typ.Final[str] = "${{ secrets.CS_ACCESS_TOKEN }}"
SKIP_REASON: typ.Final[str] = (
    "workflow directory not present in this working copy (for example inside "
    "mutmut's mutants/ sandbox, which does not copy .github/)"
)
#: The publisher's coverage selection, pinned so that both lanes changing
#: together cannot pass the parity rule unseen.
EXPECTED_SELECTION: typ.Final[dict[str, object]] = {
    "language": "python",
    "python-source": "./post_turn_quality_stop_hook",
    "output-path": "coverage.xml",
    "format": "cobertura",
    "pytest-workers": "",
    "with-ratchet": "true",
}


def fresh_documents(directory: Path = WORKFLOWS) -> Documents:
    """Read a private copy of the workflows for one test to mutate.

    Read afresh each time rather than cached for the process, so no test can
    see another's mutation and a read failure surfaces in the test that met
    it, as the reader's `WorkflowError`.

    Parameters
    ----------
    directory : Path
        The workflow directory; this repository's by default.

    Returns
    -------
    Documents
        Each workflow's parsed document, keyed by file name.

    """
    return read_workflows(directory)


def fresh_actions(root: Path = ROOT) -> Documents:
    """Read a private copy of the local actions for one test to mutate.

    Parameters
    ----------
    root : Path
        The repository root; this repository's by default.

    Returns
    -------
    Documents
        Each local action's parsed metadata, keyed by its directory.

    """
    return read_actions(root)


def assert_clean(rule: Rule, documents: Documents) -> None:
    """Fail unless a rule reports nothing."""
    found = rule(documents)
    if found:
        message = f"expected no violations, got {found}"
        raise AssertionError(message)


def assert_reports(rule: Rule, documents: Documents, fragment: str) -> None:
    """Fail unless a rule reports a violation containing a fragment."""
    found = rule(documents)
    if not any(fragment in problem for problem in found):
        message = f"expected a violation naming {fragment!r}, got {found}"
        raise AssertionError(message)


def find_publisher(documents: Documents) -> tuple[Document, Step]:
    """Return the publisher document and its upload step.

    Parameters
    ----------
    documents : Documents
        Every workflow, keyed by file name.

    Returns
    -------
    tuple of Document and Step
        The one workflow calling the uploader, and that step.

    Raises
    ------
    ValueError
        If no workflow, or more than one step, calls the uploader.

    """
    [(name, step)] = upload_steps(documents)
    return documents[name], step


def first_job(document: Document) -> dict[str, object]:
    """Return a workflow's first job.

    Parameters
    ----------
    document : Document
        The parsed workflow.

    Returns
    -------
    dict of str to object
        The first job's mapping, in declaration order.

    """
    jobs = typ.cast("dict[str, dict[str, object]]", document["jobs"])
    return next(iter(jobs.values()))


def job_steps(document: Document) -> list[Step]:
    """Return a workflow's first job's steps.

    Parameters
    ----------
    document : Document
        The parsed workflow.

    Returns
    -------
    list of Step
        The first job's step list itself, so a test can extend it.

    """
    return typ.cast("list[Step]", first_job(document)["steps"])


def coverage_step(document: Document) -> Step:
    """Return a workflow's generate-coverage step.

    Parameters
    ----------
    document : Document
        The parsed workflow.

    Returns
    -------
    Step
        The first job's first step calling `generate-coverage`.

    Raises
    ------
    StopIteration
        If the first job has no such step.

    """
    return next(
        step
        for step in job_steps(document)
        if "generate-coverage" in str(step.get("uses"))
    )


def lane_jobs(documents: Documents) -> dict[str, object]:
    """Return the pull-request lane's jobs, to add a calling job.

    Parameters
    ----------
    documents : Documents
        Every workflow, keyed by file name.

    Returns
    -------
    dict of str to object
        The lane's `jobs` mapping itself, so a test can add to it.

    """
    return typ.cast("dict[str, object]", documents[LANE]["jobs"])
