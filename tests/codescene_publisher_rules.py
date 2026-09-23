"""Hold the single push-to-main CodeScene publisher (CV-005).

One workflow, answering only a push to main (or a dispatch, which the upload's
ref guard confines to main), refreshes the ratchet baseline and uploads. Every
pull-request coverage lane ratchets against that baseline, so it must select
exactly what the publisher selects, at the same pin. The checksum machinery
the uploader has retired stays out of every workflow.
"""

from __future__ import annotations

import copy
import re
import typing as typ

from codescene_pull_request_rules import pull_request_closure
from codescene_workflow_reader import (
    Document,
    Step,
    calls,
    folded,
    jobs,
    scalars,
    steps,
    triggers,
)

COVERAGE_ACTION: typ.Final[str] = (
    "leynos/shared-actions/.github/actions/generate-coverage"
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
PULL_REQUEST_GUARD: typ.Final[str] = "github.event_name == 'pull_request'"
PINNED: typ.Final[re.Pattern[str]] = re.compile(r"@[0-9a-f]{40}")

#: Retired with CV-005 everywhere, not only on pull-request lanes: the
#: uploader rejects `installer-checksum` outright, and the variable and its
#: refresher workflow pinned an installer script the uploader no longer runs.
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
    """Return every step in any workflow that calls the CodeScene uploader."""
    return [
        (name, step)
        for name, document in documents.items()
        for step in steps(name, document)
        if calls(step, UPLOAD_ACTION)
    ]


def _publisher_triggers(name: str, document: Document) -> list[str]:
    """Report a publisher answering anything but a push to main."""
    events = triggers(name, document)
    found = [
        f"{name} must not answer {event}"
        for event in events
        if event not in {"push", "workflow_dispatch"}
    ]
    if events.get("push") != {"branches": ["main"]}:
        found.append(f"{name} must answer exactly `push: branches: [main]`")
    return found


def _declares_group(concurrency: object) -> bool:
    """Return whether a workflow-level concurrency value names a group."""
    if isinstance(concurrency, dict):
        return "group" in concurrency
    return isinstance(concurrency, str)


def _publisher_concurrency(name: str, document: Document) -> list[str]:
    """Report a publisher whose runs could overlap or be cancelled."""
    concurrency = document.get("concurrency")
    if not _declares_group(concurrency):
        return [f"{name} needs a workflow-level concurrency group"]
    scopes = [concurrency]
    scopes += [job.get("concurrency") for job in jobs(name, document).values()]
    return [
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
    return found


def _conditional_jobs(name: str, document: Document) -> list[str]:
    """Report a publisher job that could be skipped by its own condition."""
    return [
        f"{name} job {job_name} must run unconditionally"
        for job_name, job in jobs(name, document).items()
        if "if" in job
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
    """
    uploads = upload_steps(documents)
    if len(uploads) != 1:
        return [f"expected one CodeScene upload step, found {len(uploads)}"]
    name, upload = uploads[0]
    document = documents[name]
    return [
        *_publisher_triggers(name, document),
        *_publisher_concurrency(name, document),
        *_conditional_jobs(name, document),
        *_upload_step(name, upload),
        *_token_elsewhere(name, document, upload),
    ]


def coverage_steps(name: str, document: Document) -> list[Step]:
    """Return one workflow's generate-coverage steps."""
    return [step for step in steps(name, document) if calls(step, COVERAGE_ACTION)]


def _selection(step: Step) -> dict[str, object]:
    """Return a coverage step's inputs, less the artefact switch."""
    inputs = step.get("with")
    inputs = dict(inputs) if isinstance(inputs, dict) else {}
    inputs.pop("publish-artefact", None)
    return inputs


def _pull_request_lane(name: str, step: Step, trunk: Step) -> list[str]:
    """Report a pull-request coverage step that cannot ratchet like main."""
    inputs = step.get("with")
    inputs = inputs if isinstance(inputs, dict) else {}
    found: list[str] = []
    if step.get("if", PULL_REQUEST_GUARD) != PULL_REQUEST_GUARD:
        found.append(f"{name} coverage may run only as `{PULL_REQUEST_GUARD}`")
    if inputs.get("with-ratchet") != "true":
        found.append(f"{name} coverage must set with-ratchet 'true'")
    if inputs.get("publish-artefact") != "false":
        found.append(f"{name} coverage must set publish-artefact 'false'")
    if _selection(step) != _selection(trunk):
        found.append(f"{name} coverage selection differs from the publisher's")
    if step.get("uses") != trunk.get("uses"):
        found.append(f"{name} coverage pin differs from the publisher's")
    return found


def coverage_violations(documents: dict[str, Document]) -> list[str]:
    """Report coverage lanes that no longer ratchet against main's baseline.

    The publisher's generator writes the baseline and runs unconditionally;
    every pull-request generator reads it, so each must ratchet, publish no
    artefact and select exactly what the publisher selects, at the same pin.
    """
    uploads = upload_steps(documents)
    if len(uploads) != 1:
        return ["coverage lanes need exactly one publisher to compare against"]
    publisher = uploads[0][0]
    trunk_steps = coverage_steps(publisher, documents[publisher])
    if len(trunk_steps) != 1:
        return [f"{publisher} must generate coverage exactly once"]
    trunk = trunk_steps[0]
    found = [
        f"{publisher} {problem}"
        for problem, failed in (
            ("coverage must run unconditionally", "if" in trunk),
            (
                "coverage must set with-ratchet 'true'",
                _with(trunk, "with-ratchet") != "true",
            ),
            ("must pin shared actions by full SHA", not _pinned(trunk, uploads[0][1])),
            (
                "upload pin differs from its coverage pin",
                _ref(trunk) != _ref(uploads[0][1]),
            ),
        )
        if failed
    ]
    lanes = [
        (name, step)
        for name, document in pull_request_closure(documents).items()
        for step in coverage_steps(name, document)
    ]
    if not lanes:
        found.append("no pull-request lane generates coverage for the ratchet")
    for name, step in lanes:
        found += _pull_request_lane(name, step, trunk)
    return found + _baseline_writers(documents)


def _baseline_writers(documents: dict[str, Document]) -> list[str]:
    """Report a coverage step that may write the baseline off main's push.

    The default, `auto`, saves the baseline only on a push to
    `refs/heads/main`. `always` hands that restriction to the calling
    workflow, so on a pull-request lane each push could lower the baseline its
    next push ratchets against, and on the publisher a dispatch from a branch
    would write one.
    """
    return [
        f"{name} coverage must leave publish-baseline at `auto`"
        for name, document in documents.items()
        for step in coverage_steps(name, document)
        if _with(step, "publish-baseline") not in {None, "auto"}
    ]


def _with(step: Step, key: str) -> object:
    """Return one input of a step, or None."""
    inputs = step.get("with")
    return inputs.get(key) if isinstance(inputs, dict) else None


def _ref(step: Step) -> str:
    """Return the ref a step's `uses:` names."""
    return str(step.get("uses", "")).partition("@")[2]


def _pinned(*called: Step) -> bool:
    """Return whether every step pins its action by a full commit SHA."""
    return all(PINNED.fullmatch(f"@{_ref(step)}") for step in called)


def retired_names(documents: dict[str, Document]) -> list[str]:
    """Report any retired checksum input, variable or refresher workflow."""
    found = [
        f"{name} still names {retired}"
        for name, document in documents.items()
        for retired in RETIRED
        if retired in name.casefold()
        or any(retired in folded(text) for text in scalars(document))
    ]
    return sorted(set(found))
