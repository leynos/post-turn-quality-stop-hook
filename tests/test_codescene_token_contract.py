"""Prove where the publisher may bind CS_ACCESS_TOKEN, and what the jobs may hold.

The token is bound once, in a check step that runs one exact command, and
reaches the uploader only as its `access-token` input: the uploader is a
composite action, so a token in its step's `env` would reach the nested
upload-artifact and cache steps too. Every test mutates a copy of this
repository's workflows and asserts that the clause meant to catch the edit
does.
"""

from __future__ import annotations

import typing as typ

import pytest
from codescene_contract_support import (
    CREDENTIAL_REFERENCE,
    LANE,
    SKIP_REASON,
    WORKFLOWS,
    Documents,
    assert_reports,
    coverage_step,
    find_publisher,
    first_job,
    job_steps,
)
from codescene_coverage_rules import coverage_violations
from codescene_publisher_rules import publisher_violations

if typ.TYPE_CHECKING:
    from codescene_workflow_reader import Step

pytestmark = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason=SKIP_REASON)


def _check_step(documents: Documents) -> Step:
    """Return the publisher's token check step."""
    publisher, _ = find_publisher(documents)
    return next(s for s in job_steps(publisher) if s.get("id") == "codescene-token")


def test_token_cannot_return_to_the_upload_env(documents: Documents) -> None:
    """The composite uploader would pass its step env to nested steps."""
    _, upload = find_publisher(documents)
    upload["env"] = {"CS_ACCESS_TOKEN": CREDENTIAL_REFERENCE}
    assert_reports(publisher_violations, documents, "must not bind CS_ACCESS_TOKEN")


def test_check_step_cannot_bind_the_token(documents: Documents) -> None:
    """The command reads the secret by expression; no env needs to hold it."""
    _check_step(documents)["env"] = {"CS_ACCESS_TOKEN": CREDENTIAL_REFERENCE}
    assert_reports(publisher_violations, documents, "must not bind CS_ACCESS_TOKEN")


def test_token_cannot_move_to_the_job(documents: Documents) -> None:
    """A binding in a wider scope reaches every step of the job."""
    publisher, _ = find_publisher(documents)
    first_job(publisher)["env"] = {"CS_ACCESS_TOKEN": CREDENTIAL_REFERENCE}
    assert_reports(publisher_violations, documents, "must not bind CS_ACCESS_TOKEN")
    assert_reports(publisher_violations, documents, "outside its two steps")


def test_check_step_must_exist(documents: Documents) -> None:
    """Without it the guard's output is never written and the upload never runs."""
    publisher, _ = find_publisher(documents)
    job_steps(publisher).remove(_check_step(documents))
    assert_reports(publisher_violations, documents, "needs one `codescene-token`")


def test_check_step_must_precede_the_upload(documents: Documents) -> None:
    """An output written after the upload cannot enable it."""
    publisher, upload = find_publisher(documents)
    held = job_steps(publisher)
    check = _check_step(documents)
    held.remove(check)
    held.insert(held.index(upload) + 1, check)
    assert_reports(publisher_violations, documents, "must run before the upload")


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"if": "false"}, "may carry only"),
        ({"shell": "python {0}"}, "may carry only"),
        ({"continue-on-error": True}, "must not continue on error"),
        ({"env": {"X": "1"}}, "may carry only"),
        ({"run": 'echo "available=true" >> "$GITHUB_OUTPUT"'}, "must run exactly"),
        (
            {"run": "false && echo \"available=${{ secrets.CS_ACCESS_TOKEN != '' }}\""},
            "must run exactly",
        ),
    ],
)
def test_check_step_runs_one_exact_command(
    documents: Documents, change: dict[str, object], expected: str
) -> None:
    """The check step can neither be skipped nor run anything else."""
    _check_step(documents).update(change)
    assert_reports(publisher_violations, documents, expected)


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        ({"contents": "write"}, "upload job permissions"),
        (None, "upload job permissions"),
    ],
)
def test_upload_job_token_is_read_only(
    documents: Documents, permissions: object, expected: str
) -> None:
    """Nothing in the upload job writes to the repository."""
    publisher, _ = find_publisher(documents)
    first_job(publisher)["permissions"] = permissions
    assert_reports(publisher_violations, documents, expected)


def test_publisher_checkout_keeps_no_credentials(documents: Documents) -> None:
    """The coverage run executes repository and dependency code after checkout."""
    publisher, _ = find_publisher(documents)
    checkout = next(
        s for s in job_steps(publisher) if "actions/checkout" in str(s.get("uses"))
    )
    checkout.pop("with", None)
    assert_reports(publisher_violations, documents, "persist-credentials: false")


def test_pull_request_coverage_token_is_read_only(documents: Documents) -> None:
    """The pull-request lane runs contributed code with its job's token."""
    first_job(documents[LANE]).pop("permissions", None)
    assert_reports(coverage_violations, documents, "coverage job permissions")


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("path", "lcov.info", "coverage step's output-path"),
        ("format", "lcov", "coverage step's format"),
    ],
)
def test_upload_reads_what_coverage_wrote(
    documents: Documents, key: str, value: str, expected: str
) -> None:
    """An upload of another file or format publishes nothing the run measured."""
    _, upload = find_publisher(documents)
    typ.cast("dict[str, object]", upload["with"])[key] = value
    assert_reports(coverage_violations, documents, expected)


def test_pull_request_lane_cannot_upload_the_report(documents: Documents) -> None:
    """`publish-artefact: 'false'` is moot if another step uploads the report."""
    report = typ.cast("dict[str, object]", coverage_step(documents[LANE])["with"])
    job_steps(documents[LANE]).append({
        "uses": "actions/upload-artifact@v4",
        "with": {"name": "coverage", "path": report["output-path"]},
    })
    assert_reports(coverage_violations, documents, "must not upload the coverage")
