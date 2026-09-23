"""Hold the single push-to-main CodeScene publisher (CV-005).

One workflow, answering only a push to main (or a dispatch, which the upload's
ref guard confines to main), refreshes the ratchet baseline and uploads. Its
upload is bound, guarded and serialized here; the coverage lanes that ratchet
against its baseline are held in `codescene_coverage_rules`. The checksum
machinery the uploader has retired stays out of every workflow.
"""

from __future__ import annotations

import typing as typ

from codescene_token_rules import CHECK_ID, token_violations
from codescene_workflow_reader import (
    Document,
    Step,
    calls,
    continues_on_error,
    folded,
    holding_job,
    jobs,
    scalars,
    steps,
    triggers,
)

UPLOAD_ACTION: typ.Final[str] = (
    "leynos/shared-actions/.github/actions/upload-codescene-coverage"
)
#: The upload step's whole condition, as a set of conjuncts. Exact rather than
#: a superset: an extra conjunct can only narrow the upload, and `&& false`
#: narrows it to never. Exactness also refuses every `||`, because an `||`
#: leaves some conjunct unequal to both required ones.
UPLOAD_GUARD: typ.Final[frozenset[str]] = frozenset({
    f"steps.{CHECK_ID}.outputs.available == 'true'",
    "github.ref == 'refs/heads/main'",
})

#: The only token scope the upload job needs.
READ_ONLY: typ.Final[dict[str, str]] = {"contents": "read"}
CHECKOUT_ACTION: typ.Final[str] = "actions/checkout"

#: Retired with CV-005 everywhere, not only on pull-request lanes: the
#: uploader rejects `installer-checksum` outright, and the variable and its
#: refresher workflow pinned an installer script the uploader no longer runs.
#: The publisher answers these events and no others.
PUBLISHER_EVENTS: typ.Final[frozenset[str]] = frozenset({"push", "workflow_dispatch"})

#: The publisher's concurrency group, keyed on the ref alone.
PUBLISHER_GROUP: typ.Final[str] = "coverage-main-${{ github.ref }}"

RETIRED: typ.Final[tuple[str, ...]] = (
    "installer-checksum",
    "codescene_cli_sha256",
    "get-codescene-sha",
)


def _conjuncts(condition: object) -> frozenset[str]:
    """Split a step condition on `&&`, normalizing whitespace."""
    text = str(condition).strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    return frozenset(" ".join(part.split()) for part in text.split("&&"))


def upload_steps(documents: dict[str, Document]) -> list[tuple[str, Step]]:
    """Return every step in any workflow that calls the CodeScene uploader.

    Parameters
    ----------
    documents : dict of str to Document
        Every workflow in the repository, keyed by file name.

    Returns
    -------
    list of tuple of (str, Step)
        Each uploading step with its workflow's file name.

    """
    return [
        (name, step)
        for name, document in documents.items()
        for step in steps(name, document)
        if calls(step, UPLOAD_ACTION)
    ]


def _publisher_triggers(name: str, document: Document) -> list[str]:
    """Report a publisher answering anything but a push to main or a dispatch.

    The event set is pinned exactly: losing `workflow_dispatch` would leave
    automerged changes unmeasurable with nothing failing.
    """
    events = triggers(name, document)
    found = (
        [f"{name} must answer exactly {sorted(PUBLISHER_EVENTS)}, not {sorted(events)}"]
        if events.keys() != PUBLISHER_EVENTS
        else []
    )
    if events.get("push") != {"branches": ["main"]}:
        found.append(f"{name} must answer exactly `push: branches: [main]`")
    return found


def _declares_group(concurrency: object) -> bool:
    """Return whether a concurrency value names a group."""
    if isinstance(concurrency, dict):
        return "group" in concurrency
    return isinstance(concurrency, str)


def _group_is_exact(concurrency: object) -> bool:
    """Return whether a concurrency group is exactly the publisher's."""
    group = concurrency.get("group") if isinstance(concurrency, dict) else concurrency
    return " ".join(str(group).split()) == PUBLISHER_GROUP


def _publisher_concurrency(name: str, document: Document, upload: Step) -> list[str]:
    """Report a publisher whose uploads could overlap or be cancelled.

    The group must govern the upload: at workflow level or on the uploading
    job. One on an unrelated job leaves concurrent uploads possible. Every
    group, at either level, is exactly the ref-keyed one: a branch dispatch
    then queues apart from main, while every run on main shares one group, so
    runs never overlap and the survivor of any replacement is the newest
    trigger. A group keyed on the event too would let an earlier dispatch
    finish after a newer push and upload older coverage last.
    """
    governing = [
        value
        for value in (
            document.get("concurrency"),
            holding_job(name, document, upload).get("concurrency"),
        )
        if _declares_group(value)
    ]
    found = (
        []
        if governing
        else [f"{name} needs a concurrency group on the workflow or the upload job"]
    )
    scopes = [document.get("concurrency")]
    scopes += [job.get("concurrency") for job in jobs(name, document).values()]
    found += [
        f"{name} concurrency group must be exactly `{PUBLISHER_GROUP}`"
        for scope in scopes
        if _declares_group(scope) and not _group_is_exact(scope)
    ]
    return found + [
        f"{name} cancels a publisher run in progress"
        for scope in scopes
        if isinstance(scope, dict)
        and scope.get("cancel-in-progress", False) is not False
    ]


def _upload_step(name: str, step: Step) -> list[str]:
    """Report an upload step not guarded and moded as required."""
    inputs = step.get("with")
    inputs = inputs if isinstance(inputs, dict) else {}
    found: list[str] = []
    if _conjuncts(step.get("if", "")) != UPLOAD_GUARD:
        found.append(f"{name} upload must be guarded on exactly {sorted(UPLOAD_GUARD)}")
    if inputs.get("mode") != "upload":
        found.append(f"{name} upload must name `mode: upload`")
    if continues_on_error(step):
        found.append(f"{name} upload must not continue on error")
    return found


def _publisher_jobs(name: str, document: Document) -> list[str]:
    """Report a publisher job that could be skipped or could fail green."""
    return [
        f"{name} job {job_name} {problem}"
        for job_name, job in jobs(name, document).items()
        for problem, failed in (
            ("must run unconditionally", "if" in job),
            ("must not continue on error", continues_on_error(job)),
        )
        if failed
    ]


def _upload_job(name: str, document: Document, upload: Step) -> list[str]:
    """Report an upload job with a wider token or persisted Git credentials.

    Nothing in the job writes to the repository, and the coverage run executes
    repository and dependency code after checkout, so the token is read-only
    and the checkout keeps no credentials.
    """
    job = holding_job(name, document, upload)
    found = (
        []
        if job.get("permissions") == READ_ONLY
        else [f"{name} upload job permissions must be exactly {READ_ONLY}"]
    )
    return found + [
        f"{name} checkout must set persist-credentials: false"
        for step in typ.cast("list[Step]", job.get("steps", []))
        if calls(step, CHECKOUT_ACTION) and not _keeps_no_credentials(step)
    ]


def _keeps_no_credentials(checkout: Step) -> bool:
    """Return whether a checkout step sets `persist-credentials: false`."""
    inputs = checkout.get("with")
    return isinstance(inputs, dict) and inputs.get("persist-credentials") is False


def publisher_violations(documents: dict[str, Document]) -> list[str]:
    """Report anything but one guarded push-to-main publisher.

    Where the token is bound is held by `codescene_token_rules`.

    Parameters
    ----------
    documents : dict of str to Document
        Every workflow in the repository, keyed by file name.

    Returns
    -------
    list of str
        One message per violation; empty when the repository complies.

    """
    uploads = upload_steps(documents)
    if len(uploads) != 1:
        return [f"expected one CodeScene upload step, found {len(uploads)}"]
    name, upload = uploads[0]
    document = documents[name]
    return [
        *_publisher_triggers(name, document),
        *_publisher_concurrency(name, document, upload),
        *_publisher_jobs(name, document),
        *_upload_step(name, upload),
        *_upload_job(name, document, upload),
        *token_violations(name, document, upload),
    ]


def retired_names(documents: dict[str, Document]) -> list[str]:
    """Report any retired checksum input, variable or refresher workflow.

    Parameters
    ----------
    documents : dict of str to Document
        Every workflow in the repository, keyed by file name.

    Returns
    -------
    list of str
        One message per workflow and retired name; empty when none remains.

    """
    found = [
        f"{name} still names {retired}"
        for name, document in documents.items()
        for retired in RETIRED
        if retired in name.casefold()
        or any(retired in folded(text) for text in scalars(document))
    ]
    return sorted(set(found))
