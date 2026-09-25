"""Hold where the publisher puts CS_ACCESS_TOKEN (CV-005).

The uploader is a composite action. A token bound in its step's `env` would
reach its nested upload-artifact and cache steps too, and the action binds the
token itself from `inputs.access-token`. So no `env` binds the token at all: a
check step runs one exact command that writes whether the secret is set, and
the upload receives the token only as its `access-token` input.

A guard on `env.CS_ACCESS_TOKEN != ''` is simply false when the binding is
deleted or moved, so the upload would skip forever with nothing failing. The
check step and its output are therefore asserted positively, not inferred from
the guard.
"""

from __future__ import annotations

import copy
import typing as typ

from codescene_workflow_reader import (
    Document,
    Step,
    continues_on_error,
    folded,
    holding_job,
    jobs,
    scalars,
)

#: The check step's id, which the upload guard reads.
CHECK_ID: typ.Final[str] = "codescene-token"

#: The check step's whole command. The expression evaluates to `true` or
#: `false` before the shell runs, so there is no shell conditional to defeat and
#: the token is bound in no step's `env`; a fork without the secret writes
#: `false` and skips the upload rather than failing.
CHECK_COMMAND: typ.Final[str] = (
    'echo "available=${{ secrets.CS_ACCESS_TOKEN != \'\' }}" >> "$GITHUB_OUTPUT"'
)

#: What the upload passes as `access-token`.
CREDENTIAL_INPUT: typ.Final[str] = "${{ secrets.CS_ACCESS_TOKEN }}"

#: Keys a check step may carry. Anything else, such as `if`, `env`, `uses`,
#: `shell` or `continue-on-error`, could skip it, bind the token, run other
#: code, or turn its failure green.
CHECK_KEYS: typ.Final[frozenset[str]] = frozenset({"name", "id", "run"})


def expression(value: object) -> str:
    """Return an expression with its inner whitespace normalized.

    Parameters
    ----------
    value : object
        A scalar from a parsed workflow.

    Returns
    -------
    str
        The text with `${{` and `}}` spaced and runs of whitespace collapsed.

    """
    return " ".join(str(value).replace("${{", "${{ ").replace("}}", " }}").split())


def _check_step(name: str, check: Step, upload_index: int, index: int) -> list[str]:
    """Report a check step that could skip, fail green or run other code."""
    return [
        f"{name} `{CHECK_ID}` step {problem}"
        for problem, failed in (
            ("must run before the upload", index > upload_index),
            (f"may carry only {sorted(CHECK_KEYS)}", not check.keys() <= CHECK_KEYS),
            ("must not continue on error", continues_on_error(check)),
            (
                f"must run exactly `{CHECK_COMMAND}`",
                " ".join(str(check.get("run", "")).split()) != CHECK_COMMAND,
            ),
        )
        if failed
    ]


def _outside_steps(name: str, document: Document, kept: list[Step]) -> list[str]:
    """Report the token anywhere in the publisher but the named steps."""
    rest = copy.deepcopy(document)
    for job in jobs(name, rest).values():
        job["steps"] = [
            step
            for step in typ.cast("list[Step]", job.get("steps", []))
            if step not in kept
        ]
    if any("cs_access_token" in folded(text) for text in scalars(rest)):
        return [f"{name} puts CS_ACCESS_TOKEN in reach outside its two steps"]
    return []


def token_violations(name: str, document: Document, upload: Step) -> list[str]:
    """Report a token bound anywhere but the check step, or passed any other way.

    Parameters
    ----------
    name : str
        The publisher's file name, for messages.
    document : Document
        The parsed publisher.
    upload : Step
        The publisher's upload step.

    Returns
    -------
    list of str
        One message per violation; empty when the binding is as required.

    """
    held = typ.cast("list[Step]", holding_job(name, document, upload).get("steps", []))
    checks = [index for index, step in enumerate(held) if step.get("id") == CHECK_ID]
    if len(checks) != 1:
        return [f"{name} needs one `{CHECK_ID}` step in the upload job"]
    check = held[checks[0]]
    found = _check_step(name, check, held.index(upload), checks[0])
    inputs = upload.get("with")
    inputs = inputs if isinstance(inputs, dict) else {}
    if expression(inputs.get("access-token")) != CREDENTIAL_INPUT:
        found.append(f"{name} upload must pass access-token {CREDENTIAL_INPUT}")
    return (
        found
        + _env_bindings(name, document)
        + _outside_steps(name, document, [check, upload])
    )


def _env_bindings(name: str, document: Document) -> list[str]:
    """Report CS_ACCESS_TOKEN in any `env` of the publisher, at any scope.

    The upload step's is the sharp case: the uploader is composite and would
    pass it to its nested steps. A job or workflow `env` reaches every step.
    """
    scopes = [document.get("env")]
    for job in jobs(name, document).values():
        scopes.append(job.get("env"))
        scopes += [
            step.get("env") for step in typ.cast("list[Step]", job.get("steps", []))
        ]
    return [
        f"{name} must not bind CS_ACCESS_TOKEN in any env"
        for scope in scopes
        if any("cs_access_token" in folded(text) for text in scalars(scope))
    ][:1]
