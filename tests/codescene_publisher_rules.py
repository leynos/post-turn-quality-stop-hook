"""Hold the single push-to-main CodeScene publisher (CV-005).

One workflow, answering only a push to main (or a dispatch, which the upload's
ref guard confines to main), refreshes the ratchet baseline and uploads. Its
upload is bound, guarded and serialized here; the coverage lanes that ratchet
against its baseline are held in `codescene_coverage_rules`. The checksum
machinery the uploader has retired stays out of every workflow.
"""

from __future__ import annotations

import copy
import re
import typing as typ

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
    "env.CS_ACCESS_TOKEN != ''",
    "github.ref == 'refs/heads/main'",
})
CREDENTIAL_BINDING: typ.Final[str] = "${{ secrets.CS_ACCESS_TOKEN }}"
CREDENTIAL_INPUT: typ.Final[str] = "${{ env.CS_ACCESS_TOKEN }}"

#: Retired with CV-005 everywhere, not only on pull-request lanes: the
#: uploader rejects `installer-checksum` outright, and the variable and its
#: refresher workflow pinned an installer script the uploader no longer runs.
#: The publisher answers these events and no others.
PUBLISHER_EVENTS: typ.Final[frozenset[str]] = frozenset({"push", "workflow_dispatch"})

#: Expressions every publisher concurrency group must evaluate.
GROUP_KEYS: typ.Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\$\{\{\s*github\.ref\s*\}\}"),
    re.compile(r"\$\{\{\s*github\.event_name\s*\}\}"),
)

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


def _expression(value: object) -> str:
    """Return an expression with its inner whitespace normalized."""
    return " ".join(str(value).replace("${{", "${{ ").replace("}}", " }}").split())


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


def _group_is_keyed(concurrency: object) -> bool:
    """Return whether a concurrency group evaluates both the ref and the event.

    Matched as expressions, not words: a literal
    `coverage-main-github.ref-github.event_name` names both and evaluates
    neither, so every run would still share one group.
    """
    group = concurrency.get("group") if isinstance(concurrency, dict) else concurrency
    return all(pattern.search(str(group)) for pattern in GROUP_KEYS)


def _publisher_concurrency(name: str, document: Document, upload: Step) -> list[str]:
    """Report a publisher whose uploads could overlap or be cancelled.

    The group must govern the upload: at workflow level or on the uploading
    job. One on an unrelated job leaves concurrent uploads possible. Every
    group, at either level, must be keyed on the ref and the event: a newer
    run replaces a pending one in the same group whatever `cancel-in-progress`
    says, so a branch dispatch, which neither uploads nor writes a baseline,
    or a dispatch on main, which uploads but writes no baseline, could
    otherwise displace a pending push to main.
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
        f"{name} concurrency group must be keyed on github.ref and github.event_name"
        for scope in scopes
        if _declares_group(scope) and not _group_is_keyed(scope)
    ]
    return found + [
        f"{name} cancels a publisher run in progress"
        for scope in scopes
        if isinstance(scope, dict)
        and scope.get("cancel-in-progress", False) is not False
    ]


def _upload_step(name: str, step: Step) -> list[str]:
    """Report an upload step not bound, guarded and moded as required."""
    env = step.get("env")
    inputs = step.get("with")
    env = env if isinstance(env, dict) else {}
    inputs = inputs if isinstance(inputs, dict) else {}
    found: list[str] = []
    if _conjuncts(step.get("if", "")) != UPLOAD_GUARD:
        found.append(f"{name} upload must be guarded on exactly {sorted(UPLOAD_GUARD)}")
    if _expression(env.get("CS_ACCESS_TOKEN")) != CREDENTIAL_BINDING:
        found.append(f"{name} upload step must bind {CREDENTIAL_BINDING}")
    if _expression(inputs.get("access-token")) != CREDENTIAL_INPUT:
        found.append(f"{name} upload must pass access-token {CREDENTIAL_INPUT}")
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


def _token_elsewhere(name: str, document: Document, upload: Step) -> list[str]:
    """Report the token anywhere in the publisher but its upload step."""
    rest = copy.deepcopy(document)
    for job in jobs(name, rest).values():
        job["steps"] = [
            s for s in typ.cast("list[Step]", job.get("steps", [])) if s != upload
        ]
    if any("cs_access_token" in folded(text) for text in scalars(rest)):
        return [f"{name} puts CS_ACCESS_TOKEN in reach outside the upload step"]
    return []


def publisher_violations(documents: dict[str, Document]) -> list[str]:
    """Report anything but one guarded push-to-main publisher.

    The binding is asserted positively. A guard on `env.CS_ACCESS_TOKEN` is
    simply false when the binding is deleted or moved, so the upload would
    skip forever with nothing failing.

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
        *_token_elsewhere(name, document, upload),
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
