"""Shared inputs for the workflow contract tests.

The contract tests mutate a copy of this repository's workflows in the way a
later edit could, and assert that the clause meant to catch that edit does.
These helpers hand each test its own copy and find the parts they mutate.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

from workflow_files import read_workflows
from workflow_reader import Document, Step

if typ.TYPE_CHECKING:
    import collections.abc as cabc

type Documents = dict[str, Document]
type Rule = cabc.Callable[[Documents], list[str]]

ROOT: typ.Final[Path] = Path(__file__).resolve().parents[1]
WORKFLOWS: typ.Final[Path] = ROOT / ".github" / "workflows"
#: The pull-request lane, which the mutation cases extend.
LANE: typ.Final[str] = "ci.yml"
SKIP_REASON: typ.Final[str] = (
    "workflow directory not present in this working copy (for example inside "
    "mutmut's mutants/ sandbox, which does not copy .github/)"
)


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
    jobs = typ.cast("dict[str, dict[str, object]]", document["jobs"])
    return typ.cast("list[Step]", next(iter(jobs.values()))["steps"])


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
