"""Prove the publisher half of CV-005: one guarded push-to-main uploader.

The first test judges this repository's workflows. Every other test mutates a
copy of them in the way a later edit could and asserts that the clause meant to
catch that edit does.
"""

from __future__ import annotations

import copy
import typing as typ

import pytest
from codescene_contract_support import (
    CREDENTIAL_REFERENCE,
    LANE,
    SKIP_REASON,
    WORKFLOWS,
    Documents,
    coverage_step,
    find_publisher,
    first_job,
    fresh_documents,
    job_steps,
)
from codescene_publisher_rules import (
    coverage_violations,
    publisher_violations,
    retired_names,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

DISPATCH = "github.event_name == 'workflow_dispatch'"
MAIN_GUARD = "env.CS_ACCESS_TOKEN != '' && github.ref == 'refs/heads/main'"

pytestmark = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason=SKIP_REASON)


@pytest.fixture
def documents() -> Documents:
    """Give each test its own copy of the workflows to mutate."""
    return fresh_documents()


def test_repository_has_one_guarded_publisher(documents: Documents) -> None:
    """Hold every publisher clause over the workflows as committed."""
    assert publisher_violations(documents) == []
    assert coverage_violations(documents) == []
    assert retired_names(documents) == []


@pytest.mark.parametrize(
    "guard",
    [
        "env.CS_ACCESS_TOKEN != ''",
        "github.ref == 'refs/heads/main'",
        f"{MAIN_GUARD} || {DISPATCH}",
        # The discriminating case: both required conjuncts stay whole and the
        # `||` hides inside an extra one. Exact conjunct equality refuses it,
        # which is why this contract needs no separate `||` scan.
        f"{MAIN_GUARD} && github.actor != 'x' || {DISPATCH}",
        f"{MAIN_GUARD} && false",
    ],
)
def test_upload_guard_is_exactly_token_and_main(
    documents: Documents, guard: str
) -> None:
    """The upload runs only with the token, and only for main."""
    _, upload = find_publisher(documents)
    upload["if"] = guard
    assert any("upload must be guarded" in p for p in publisher_violations(documents))


def test_upload_guard_accepts_the_expression_wrapper(documents: Documents) -> None:
    """`${{ }}` around the condition is the same condition."""
    _, upload = find_publisher(documents)
    upload["if"] = "${{ github.ref == 'refs/heads/main' && env.CS_ACCESS_TOKEN != '' }}"
    assert publisher_violations(documents) == []


@pytest.mark.parametrize(
    ("concurrency", "expected"),
    [
        ({"group": "coverage-main", "cancel-in-progress": True}, "cancels"),
        ({"group": "coverage-main", "cancel-in-progress": "${{ true }}"}, "cancels"),
        (None, "needs a workflow-level concurrency group"),
    ],
)
def test_publisher_never_cancels(
    documents: Documents, concurrency: object, expected: str
) -> None:
    """A cancelled publisher abandons its upload and its baseline write."""
    publisher, _ = find_publisher(documents)
    publisher["concurrency"] = concurrency
    assert any(expected in p for p in publisher_violations(documents))


def test_publisher_job_cannot_cancel(documents: Documents) -> None:
    """A job-level concurrency block cancels just as well."""
    publisher, _ = find_publisher(documents)
    first_job(publisher)["concurrency"] = {"group": "g", "cancel-in-progress": True}
    assert any("cancels" in p for p in publisher_violations(documents))


@pytest.mark.parametrize(
    "on",
    [
        {"push": {"branches": ["main"]}, "pull_request": None},
        {"push": {"branches": ["**"]}},
        {"push": {"tags": ["v*"]}},
        {"workflow_dispatch": None},
    ],
)
def test_publisher_answers_only_a_push_to_main(
    documents: Documents, on: object
) -> None:
    """Only main's pushes may write the baseline CodeScene is given."""
    publisher, _ = find_publisher(documents)
    publisher[True] = on
    assert any("must" in p for p in publisher_violations(documents))


def test_publisher_job_runs_unconditionally(documents: Documents) -> None:
    """A job-level `if: false` skips the upload with every step intact."""
    publisher, _ = find_publisher(documents)
    first_job(publisher)["if"] = "false"
    assert any("must run unconditionally" in p for p in publisher_violations(documents))


def test_token_binding_is_asserted_positively(documents: Documents) -> None:
    """Deleting the binding makes the guard false, and the upload skips forever."""
    _, upload = find_publisher(documents)
    del upload["env"]
    assert any("must bind" in p for p in publisher_violations(documents))


def test_token_binding_cannot_move_to_the_job(documents: Documents) -> None:
    """A binding in a wider scope reaches every step of the job."""
    publisher, upload = find_publisher(documents)
    upload["env"] = {}
    first_job(publisher)["env"] = {"CS_ACCESS_TOKEN": CREDENTIAL_REFERENCE}
    found = publisher_violations(documents)
    assert any("must bind" in p for p in found)
    assert any("outside the upload step" in p for p in found)


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("access-token", "${{ secrets.OTHER }}", "must pass access-token"),
        ("mode", "check", "must name `mode: upload`"),
    ],
)
def test_upload_inputs_are_asserted(
    documents: Documents, key: str, value: str, expected: str
) -> None:
    """The upload passes the bound token, and says it uploads."""
    _, upload = find_publisher(documents)
    typ.cast("dict[str, object]", upload["with"])[key] = value
    assert any(expected in p for p in publisher_violations(documents))


def test_second_uploader_is_refused(documents: Documents) -> None:
    """A second uploader could publish where nothing here looks."""
    publisher, upload = find_publisher(documents)
    job_steps(publisher).append(copy.deepcopy(upload))
    assert publisher_violations(documents) == [
        "expected one CodeScene upload step, found 2"
    ]


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("with-ratchet", "false", "with-ratchet 'true'"),
        ("publish-artefact", "true", "publish-artefact 'false'"),
        ("python-source", "./tests", "selection differs"),
    ],
)
def test_pull_request_coverage_ratchets_like_main(
    documents: Documents, key: str, value: str, expected: str
) -> None:
    """The PR lane ratchets against main's baseline and publishes nothing."""
    typ.cast("dict[str, object]", coverage_step(documents[LANE])["with"])[key] = value
    assert any(expected in p for p in coverage_violations(documents))


def test_pull_request_coverage_cannot_be_switched_off(documents: Documents) -> None:
    """`if: false` keeps the step while the ratchet never runs."""
    coverage_step(documents[LANE])["if"] = "false"
    assert any("may run only as" in p for p in coverage_violations(documents))


def test_pull_request_coverage_must_exist(documents: Documents) -> None:
    """Deleting the PR coverage step deletes the ratchet."""
    steps = job_steps(documents[LANE])
    steps.remove(coverage_step(documents[LANE]))
    assert "no pull-request lane generates coverage for the ratchet" in (
        coverage_violations(documents)
    )


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"if": "false"}, "must run unconditionally"),
        (
            {"uses": "leynos/shared-actions/.github/actions/generate-coverage@main"},
            "full SHA",
        ),
    ],
)
def test_publisher_coverage_is_pinned_and_unconditional(
    documents: Documents, change: dict[str, object], expected: str
) -> None:
    """The baseline writer always runs, at the uploader's full-SHA pin."""
    publisher, _ = find_publisher(documents)
    coverage_step(publisher).update(change)
    assert any(expected in p for p in coverage_violations(documents))


@pytest.mark.parametrize("lane", ["publisher", "pull request"])
def test_only_mains_push_writes_the_baseline(documents: Documents, lane: str) -> None:
    """`publish-baseline: always` lets a branch write the ratchet baseline."""
    document = find_publisher(documents)[0] if lane == "publisher" else documents[LANE]
    inputs = typ.cast("dict[str, object]", coverage_step(document)["with"])
    inputs["publish-baseline"] = "always"
    assert any("publish-baseline" in p for p in coverage_violations(documents))


def _restore_refresher(documents: Documents) -> None:
    """Bring back the workflow that refreshed the installer checksum."""
    documents["get-codescene-sha.yml"] = {True: "workflow_dispatch", "jobs": {}}


def _restore_installer_checksum(documents: Documents) -> None:
    """Pass the uploader the input it now rejects."""
    _, upload = find_publisher(documents)
    typ.cast("dict[str, object]", upload["with"])["installer-checksum"] = "abc"


def _restore_variable(documents: Documents) -> None:
    """Read the retired checksum variable into a lane."""
    documents[LANE]["env"] = {"CODESCENE_CLI_SHA256": "${{ vars.X }}"}


@pytest.mark.parametrize(
    "mutation",
    [_restore_refresher, _restore_installer_checksum, _restore_variable],
)
def test_retired_checksum_machinery_stays_gone(
    documents: Documents, mutation: cabc.Callable[[Documents], None]
) -> None:
    """The retired installer checksum is refused wherever it reappears."""
    mutation(documents)
    assert retired_names(documents) != []
