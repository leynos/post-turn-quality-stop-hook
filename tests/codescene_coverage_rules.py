"""Hold the coverage lanes to main's ratchet baseline (CV-005).

The publisher's generator writes the ratchet baseline on a push to main, and
every pull-request generator compares against it. So each pull-request lane
must ratchet, publish no artefact and select exactly what the publisher
selects, at the same pin; and nothing else may write a baseline.
"""

from __future__ import annotations

import re
import typing as typ

from codescene_publisher_rules import upload_steps
from codescene_pull_request_rules import closure, pull_request_closure
from codescene_workflow_reader import (
    Document,
    Step,
    calls,
    continues_on_error,
    holding_job,
    steps,
    triggers,
)

COVERAGE_ACTION: typ.Final[str] = (
    "leynos/shared-actions/.github/actions/generate-coverage"
)
PULL_REQUEST_GUARD: typ.Final[str] = "github.event_name == 'pull_request'"
PINNED: typ.Final[re.Pattern[str]] = re.compile(r"@[0-9a-f]{40}")


def coverage_steps(name: str, document: Document) -> list[Step]:
    """Return one workflow's generate-coverage steps.

    Parameters
    ----------
    name : str
        The workflow's file name, for messages.
    document : Document
        The parsed workflow.

    Returns
    -------
    list of Step
        Every step calling the shared coverage action, in order.

    """
    return [step for step in steps(name, document) if calls(step, COVERAGE_ACTION)]


def _selection(step: Step) -> dict[str, object]:
    """Return a coverage step's inputs, less the artefact switch."""
    inputs = step.get("with")
    inputs = dict(inputs) if isinstance(inputs, dict) else {}
    inputs.pop("publish-artefact", None)
    return inputs


def _pull_request_lane(
    name: str, document: Document, step: Step, trunk: Step
) -> list[str]:
    """Report a pull-request coverage step that cannot ratchet like main."""
    inputs = step.get("with")
    inputs = inputs if isinstance(inputs, dict) else {}
    found = [
        f"{name} coverage must not continue on error"
        for scope in (step, holding_job(name, document, step))
        if continues_on_error(scope)
    ]
    # Required, not merely permitted: the lane's workflow also answers a push
    # to main, and an unguarded step would then write a second baseline there,
    # outside the publisher's concurrency group.
    if step.get("if") != PULL_REQUEST_GUARD:
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


def _trunk_violations(publisher: str, trunk: Step, upload: Step) -> list[str]:
    """Report a baseline writer that could skip, fail green or drift its pin."""
    return [
        f"{publisher} {problem}"
        for problem, failed in (
            ("coverage must run unconditionally", "if" in trunk),
            ("coverage must not continue on error", continues_on_error(trunk)),
            (
                "coverage must set with-ratchet 'true'",
                _with(trunk, "with-ratchet") != "true",
            ),
            ("must pin shared actions by full SHA", not _pinned(trunk, upload)),
            ("upload pin differs from its coverage pin", _ref(trunk) != _ref(upload)),
        )
        if failed
    ]


def coverage_violations(documents: dict[str, Document]) -> list[str]:
    """Report coverage lanes that no longer ratchet against main's baseline.

    The publisher's generator writes the baseline and runs unconditionally;
    every pull-request generator reads it, so each must ratchet, publish no
    artefact and select exactly what the publisher selects, at the same pin.
    Nothing else a push starts may generate coverage, and no step may widen
    when the baseline is written.

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
        return ["coverage lanes need exactly one publisher to compare against"]
    publisher = uploads[0][0]
    trunk_steps = coverage_steps(publisher, documents[publisher])
    if len(trunk_steps) != 1:
        return [f"{publisher} must generate coverage exactly once"]
    trunk = trunk_steps[0]
    found = _trunk_violations(publisher, trunk, uploads[0][1])
    lanes = [
        (name, document, step)
        for name, document in pull_request_closure(documents).items()
        for step in coverage_steps(name, document)
    ]
    if not lanes:
        found.append("no pull-request lane generates coverage for the ratchet")
    for name, document, step in lanes:
        found += _pull_request_lane(name, document, step, trunk)
    return found + _baseline_writers(documents) + _push_writers(documents, publisher)


def _push_writers(documents: dict[str, Document], publisher: str) -> list[str]:
    """Report coverage a push can run anywhere but the publisher.

    Such a step writes a second baseline on every push to main, outside the
    publisher's concurrency group. The push side is followed through local
    calls as the pull-request side is, since a called workflow runs on its
    caller's push.
    """
    seeds = {
        name
        for name, document in documents.items()
        if name != publisher and "push" in triggers(name, document)
    }
    return [
        f"{name} coverage can run on a push; guard it to pull requests"
        for name, document in closure(seeds, documents).items()
        if name != publisher
        for step in coverage_steps(name, document)
        if step.get("if") != PULL_REQUEST_GUARD
    ]


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
