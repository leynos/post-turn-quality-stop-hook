"""Hold the CodeScene token's environment to the uploading jobs (CV-005).

The token lives in the `codescene` environment, whose deployment policy admits
`main` alone. So every job that calls the uploader declares that environment,
no other job does, and no workflow a pull request can start declares it in any
job: a declaration there would let branch code ask for the token.
"""

from __future__ import annotations

import typing as typ

from codescene_publisher_rules import upload_steps
from codescene_pull_request_rules import pull_request_closure
from codescene_workflow_reader import Document, holding_job, jobs

ENVIRONMENT: typ.Final[str] = "codescene"
MISSING: typ.Final[str] = f"the uploading job must declare `environment: {ENVIRONMENT}`"
STRAY: typ.Final[str] = f"declares `{ENVIRONMENT}` but uploads nothing"
REACHABLE: typ.Final[str] = (
    f"is reachable from a pull request and declares `{ENVIRONMENT}`"
)


def environment_name(job: dict[str, object]) -> str | None:
    """Return the environment a job declares, from either accepted form.

    Examples
    --------
    >>> environment_name({"environment": "codescene"})
    'codescene'
    >>> environment_name({"environment": {"name": "codescene", "url": "x"}})
    'codescene'
    >>> environment_name({}) is None
    True

    """
    match job.get("environment"):
        case str() as name:
            return name
        case {"name": str() as name}:
            return name
        case _:
            return None


def _uploader_violations(documents: dict[str, Document]) -> tuple[list[str], set[int]]:
    """Check each uploading job; return problems and the jobs' identities."""
    uploads = upload_steps(documents)
    if not uploads:
        return ["no workflow job calls the CodeScene uploader"], set()
    problems = []
    holders = set()
    for name, step in uploads:
        job = holding_job(name, documents[name], step)
        holders.add(id(job))
        if environment_name(job) != ENVIRONMENT:
            problems.append(f"{name}: {MISSING}")
    return problems, holders


def environment_violations(documents: dict[str, Document]) -> list[str]:
    """Report every departure from the `codescene` environment placement.

    Returns
    -------
    list of str
        One message per violation; empty when the placement holds.

    """
    problems, holders = _uploader_violations(documents)
    for name, document in documents.items():
        for job_id, job in jobs(name, document).items():
            if id(job) not in holders and environment_name(job) == ENVIRONMENT:
                problems.append(f"{name}:{job_id} {STRAY}")
    for name, document in pull_request_closure(documents).items():
        for job_id, job in jobs(name, document).items():
            if environment_name(job) == ENVIRONMENT:
                problems.append(f"{name}:{job_id} {REACHABLE}")
    return problems
