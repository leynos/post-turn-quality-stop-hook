"""Shared inputs for the CV-005 contract tests.

The contract tests mutate a copy of this repository's workflows in the way a
later edit could, and assert that the clause meant to catch that edit does.
These helpers hand each test its own copy and find the parts they mutate.
"""

from __future__ import annotations

import copy
import functools
import typing as typ
from pathlib import Path

from codescene_publisher_rules import upload_steps
from codescene_workflow_reader import Document, Step, read_workflows

type Documents = dict[str, Document]

WORKFLOWS: typ.Final[Path] = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows"
)
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


@functools.cache
def _repository() -> Documents:
    """Read this repository's workflows once per test process."""
    return read_workflows(WORKFLOWS)


def fresh_documents() -> Documents:
    """Return a private copy of this repository's workflows to mutate."""
    return copy.deepcopy(_repository())


def find_publisher(documents: Documents) -> tuple[Document, Step]:
    """Return the publisher document and its upload step."""
    [(name, step)] = upload_steps(documents)
    return documents[name], step


def first_job(document: Document) -> dict[str, object]:
    """Return a workflow's first job."""
    jobs = typ.cast("dict[str, dict[str, object]]", document["jobs"])
    return next(iter(jobs.values()))


def job_steps(document: Document) -> list[Step]:
    """Return a workflow's first job's steps."""
    return typ.cast("list[Step]", first_job(document)["steps"])


def coverage_step(document: Document) -> Step:
    """Return a workflow's generate-coverage step."""
    return next(
        step
        for step in job_steps(document)
        if "generate-coverage" in str(step.get("uses"))
    )


def lane_jobs(documents: Documents) -> dict[str, object]:
    """Return the pull-request lane's jobs, to add a calling job."""
    return typ.cast("dict[str, object]", documents[LANE]["jobs"])
