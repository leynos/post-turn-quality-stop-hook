"""Hold every CodeScene contact off the pull-request lanes (CV-005).

CV-005 moves every CodeScene call off the pull-request lanes. A pull request
generates coverage for its own ratchet and nothing else. The reason is the call
rather than the artefact: the shared uploader pins the cs-coverage archive by
digest, but the client talks to CodeScene's API and refuses to run when that
answer changes shape, which has happened twice. Keeping the call on the trunk
keeps such a change off every pull request's critical path.

Each rule is a pure function from parsed workflow documents to a list of
violations.
"""

from __future__ import annotations

import typing as typ
from pathlib import PurePosixPath

from codescene_workflow_reader import (
    Document,
    WorkflowError,
    folded,
    jobs,
    scalars,
    steps,
    triggers,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: This repository, for refusing a qualified call to one of its own workflows.
REPOSITORY: typ.Final[str] = "leynos/post-turn-quality-stop-hook"
WORKFLOW_PREFIX: typ.Final[str] = ".github/workflows/"
#: Events that start a workflow for a pull request: its head, its queued
#: merge, a review of it, or a comment on it. The review and comment events and
#: `merge_group` run with the repository's secrets for a same-repository pull
#: request.
PULL_REQUEST_EVENTS: typ.Final[frozenset[str]] = frozenset({
    "issue_comment",
    "merge_group",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_target",
})

#: What no pull-request-reachable scalar may contain, case-folded and with
#: whitespace removed. The token's name catches an `env` key, a reference in a
#: script, an input or an env value under any key, a named `secrets:`
#: forwarding and a `workflow_call` secret declaration alike; indexed and
#: serialized `secrets` reach it without spelling the name.
PULL_REQUEST_FORBIDDEN: typ.Final[tuple[tuple[str, str], ...]] = (
    ("codescene.io", "names the CodeScene host"),
    ("upload-codescene-coverage", "calls the CodeScene uploader"),
    ("cs-coverage", "names the cs-coverage client"),
    ("cs_access_token", "puts CS_ACCESS_TOKEN in reach"),
    ("secrets[", "indexes the secrets context"),
    ("tojson(secrets", "serializes the secrets context"),
)


def _local_path(reference: str, kind: str) -> str:
    """Return a `uses:` reference without its local prefix, refusing a ref.

    A qualified reference to this repository runs its file at that ref rather
    than the one checked out, and a `$/` reference carries no ref, so both are
    refused rather than followed.
    """
    if reference.casefold().startswith(f"{REPOSITORY}/".casefold()):
        message = f"{reference} runs this repository's {kind} at a ref"
        raise WorkflowError(message)
    if reference.startswith("$/") and "@" in reference:
        message = f"{reference}: a `$/` call cannot name a ref"
        raise WorkflowError(message)
    return reference.removeprefix("./").removeprefix("$/")


def local_callee(reference: str, documents: dict[str, Document]) -> str | None:
    """Return the workflow file a job-level `uses:` names in this tree.

    Matched by shape: strip a leading `./` or `$/` and ask whether the rest is
    a file under the workflow directory. A `$/` call carries no ref, and a
    qualified call to this repository runs the file at that ref rather than
    the one checked out, so both are refused rather than followed.

    Parameters
    ----------
    reference : str
        A job's `uses:` value.
    documents : dict of str to Document
        Every workflow in the repository, keyed by file name.

    Returns
    -------
    str or None
        The called workflow's file name, or None for a call to another
        repository.

    Raises
    ------
    WorkflowError
        If the call is a qualified self-call, a `$/` call with a ref, or names
        a local workflow that does not exist.

    """
    path = _local_path(reference, "workflow")
    if not path.startswith(WORKFLOW_PREFIX):
        return None
    callee = path.removeprefix(WORKFLOW_PREFIX)
    if callee not in documents:
        message = f"{reference} names no workflow in this repository"
        raise WorkflowError(message)
    return callee


def local_action(reference: str, actions: dict[str, Document]) -> str | None:
    """Return the local action a step's `uses:` names in this tree.

    Matched by shape, as `local_callee` matches a workflow: a `./` or `$/`
    reference names the directory holding the action's metadata. The path is
    normalized first, so `./a/./b/` and `./a/b` name the same action.

    Parameters
    ----------
    reference : str
        A step's `uses:` value.
    actions : dict of str to Document
        Every local action in the repository, keyed by its directory.

    Returns
    -------
    str or None
        The action's directory, or None for another repository's action.

    Raises
    ------
    WorkflowError
        If the reference is a qualified self-reference, a `$/` reference with a
        ref, or names a local directory holding no action.

    """
    path = _local_path(reference, "action")
    if not reference.startswith(("./", "$/")):
        return None
    path = PurePosixPath(path).as_posix()
    if path not in actions:
        message = f"{reference} names no action in this repository"
        raise WorkflowError(message)
    return path


def _uses(held: cabc.Iterable[object]) -> list[str]:
    """Return the `uses:` references among some steps."""
    return [
        str(typ.cast("dict[str, object]", step)["uses"])
        for step in held
        if isinstance(step, dict) and "uses" in step
    ]


def _action_steps(action: Document) -> list[object]:
    """Return a composite action's steps; other kinds run no steps here."""
    runs = action.get("runs")
    held = runs.get("steps", []) if isinstance(runs, dict) else []
    return typ.cast("list[object]", held)


def action_closure(
    documents: dict[str, Document], actions: dict[str, Document]
) -> dict[str, Document]:
    """Return every local action the workflows' steps run, transitively.

    A composite action's steps may run further local actions, so those are
    followed until nothing new is reached.

    Parameters
    ----------
    documents : dict of str to Document
        The workflows whose steps to follow, keyed by file name.
    actions : dict of str to Document
        Every local action in the repository, keyed by its directory.

    Returns
    -------
    dict of str to Document
        Each reached action's metadata, keyed by its directory.

    Raises
    ------
    WorkflowError
        If a local action reference cannot be followed; see `local_action`.

    """
    pending = [
        reference
        for name, document in documents.items()
        for reference in _uses(steps(name, document))
    ]
    found: dict[str, Document] = {}
    while pending:
        path = local_action(pending.pop(), actions)
        if path is not None and path not in found:
            found[path] = actions[path]
            pending += _uses(_action_steps(actions[path]))
    return dict(sorted(found.items()))


def _callees(name: str, documents: dict[str, Document]) -> set[str]:
    """Return the local workflows one workflow's jobs call."""
    references = (job.get("uses") for job in jobs(name, documents[name]).values())
    return {
        callee
        for reference in references
        if isinstance(reference, str)
        and (callee := local_callee(reference, documents)) is not None
    }


def _chained(found: set[str], documents: dict[str, Document]) -> set[str]:
    """Return workflows a `workflow_run` trigger chains onto any found one."""
    # GitHub matches `workflows:` on a workflow's `name:`, or on its path from
    # the repository root when it declares none.
    watched_names = {
        str(documents[name].get("name", f"{WORKFLOW_PREFIX}{name}")) for name in found
    }
    chained: set[str] = set()
    for name, document in documents.items():
        run = triggers(name, document).get("workflow_run")
        watched = run.get("workflows", []) if isinstance(run, dict) else []
        if watched_names.intersection(map(str, typ.cast("list[object]", watched))):
            chained.add(name)
    return chained


def closure(seeds: set[str], documents: dict[str, Document]) -> dict[str, Document]:
    """Return the seeds and every workflow they start, transitively.

    A workflow declaring only `workflow_call` still runs when a seed's job
    calls it, and a `workflow_run` chained onto a seed runs after it, so both
    are followed until nothing new is reached.

    Parameters
    ----------
    seeds : set of str
        File names of the workflows to start from.
    documents : dict of str to Document
        Every workflow in the repository, keyed by file name.

    Returns
    -------
    dict of str to Document
        The seeds and everything they reach, keyed by file name.

    Raises
    ------
    WorkflowError
        If a local call cannot be followed; see `local_callee`.

    """
    found = set(seeds)
    while True:
        grown = found | _chained(found, documents)
        grown |= {callee for name in grown for callee in _callees(name, documents)}
        if grown == found:
            return {name: documents[name] for name in sorted(found)}
        found = grown


def _push_is_trunk_or_tags(push: object) -> bool:
    """Return whether a push filter admits only main, or only tags.

    Any other push runs for a same-repository pull request's head branch, with
    the repository's secrets, so it belongs to the pull-request surface.
    """
    if not isinstance(push, dict):
        return False
    if push == {"branches": ["main"]}:
        return True
    return "tags" in push and not {"branches", "branches-ignore"} & push.keys()


def serves_pull_requests(name: str, document: Document) -> bool:
    """Return whether a pull request can start a workflow by its own trigger.

    Parameters
    ----------
    name : str
        The workflow's file name, for messages.
    document : Document
        The parsed workflow.

    Returns
    -------
    bool
        True for a pull-request event, or a push not confined to main or tags.

    """
    events = triggers(name, document)
    if PULL_REQUEST_EVENTS & events.keys():
        return True
    return "push" in events and not _push_is_trunk_or_tags(events["push"])


def pull_request_closure(documents: dict[str, Document]) -> dict[str, Document]:
    """Return every workflow a pull request can start, directly or not.

    A called workflow receives the token through `secrets: inherit`, so the
    rules below read the transitive closure, not a trigger list.

    Parameters
    ----------
    documents : dict of str to Document
        Every workflow in the repository, keyed by file name.

    Returns
    -------
    dict of str to Document
        Every workflow a pull-request event starts, and everything those reach.

    Raises
    ------
    WorkflowError
        If no workflow answers a pull-request event, which means the reader is
        broken rather than the repository compliant.

    """
    seeds = {
        name
        for name, document in documents.items()
        if serves_pull_requests(name, document)
    }
    if not seeds:
        message = "no workflow serves a pull request; the reader is broken"
        raise WorkflowError(message)
    return closure(seeds, documents)


def _forbidden(name: str, document: Document) -> list[str]:
    """Report each forbidden marker among one document's scalars."""
    texts = {folded(text) for text in scalars(document)}
    return [
        f"{name} {reason}"
        for marker, reason in PULL_REQUEST_FORBIDDEN
        if any(marker in text for text in texts)
    ]


def pull_request_contacts(
    documents: dict[str, Document], actions: dict[str, Document]
) -> list[str]:
    """Report every way a pull request could reach CodeScene or its token.

    Every scalar is read, keys included, at every scope, so a workflow-level
    `defaults.run.shell`, an env value under an unrelated key or a callee's
    secret declaration is seen as readily as a step's script. Every local
    action a reached step runs is read the same way. The parser discards
    comments, so prose explaining the policy is not a violation.

    Parameters
    ----------
    documents : dict of str to Document
        Every workflow in the repository, keyed by file name.
    actions : dict of str to Document
        Every local action in the repository, keyed by its directory.

    Returns
    -------
    list of str
        One message per violation; empty when the repository complies.

    Raises
    ------
    WorkflowError
        If a workflow or action reference cannot be followed.

    """
    reached = pull_request_closure(documents)
    found: list[str] = []
    for name, action in action_closure(reached, actions).items():
        found += _forbidden(name, action)
    for name, document in reached.items():
        found += _forbidden(name, document)
        found += [
            f"{name} job {job_name} forwards every secret with `secrets: inherit`"
            for job_name, job in jobs(name, document).items()
            if job.get("secrets") == "inherit"
        ]
    return found
