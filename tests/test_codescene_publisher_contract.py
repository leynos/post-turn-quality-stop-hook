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
    job_steps,
)
from codescene_coverage_rules import coverage_violations
from codescene_publisher_rules import publisher_violations, retired_names

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    type Rule = cabc.Callable[[Documents], list[str]]

DISPATCH = "github.event_name == 'workflow_dispatch'"
MAIN_GUARD = "env.CS_ACCESS_TOKEN != '' && github.ref == 'refs/heads/main'"

pytestmark = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason=SKIP_REASON)


def _assert_clean(rule: Rule, documents: Documents) -> None:
    """Assert that a rule reports nothing."""
    found = rule(documents)
    assert found == [], f"expected no violations, got {found}"


def _assert_reports(rule: Rule, documents: Documents, fragment: str) -> None:
    """Assert that a rule reports a violation containing a fragment."""
    found = rule(documents)
    assert any(fragment in problem for problem in found), (
        f"expected a violation naming {fragment!r}, got {found}"
    )


def test_repository_has_one_guarded_publisher(documents: Documents) -> None:
    """Hold every publisher clause over the workflows as committed."""
    for rule in (publisher_violations, coverage_violations, retired_names):
        _assert_clean(rule, documents)


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
    _assert_reports(publisher_violations, documents, "upload must be guarded")


def test_upload_guard_accepts_the_expression_wrapper(documents: Documents) -> None:
    """`${{ }}` around the condition is the same condition."""
    _, upload = find_publisher(documents)
    upload["if"] = "${{ github.ref == 'refs/heads/main' && env.CS_ACCESS_TOKEN != '' }}"
    _assert_clean(publisher_violations, documents)


@pytest.mark.parametrize(
    ("concurrency", "expected"),
    [
        ({"group": "coverage-main", "cancel-in-progress": True}, "cancels"),
        ({"group": "coverage-main", "cancel-in-progress": "${{ true }}"}, "cancels"),
        (None, "needs a concurrency group on the workflow or the upload job"),
    ],
)
def test_publisher_never_cancels(
    documents: Documents, concurrency: object, expected: str
) -> None:
    """A cancelled publisher abandons its upload and its baseline write."""
    publisher, _ = find_publisher(documents)
    publisher["concurrency"] = concurrency
    _assert_reports(publisher_violations, documents, expected)


def test_upload_job_group_governs_the_upload(documents: Documents) -> None:
    """A group on the uploading job serves as well as a workflow-level one."""
    publisher, _ = find_publisher(documents)
    first_job(publisher)["concurrency"] = publisher.pop("concurrency")
    _assert_clean(publisher_violations, documents)


def test_unrelated_job_group_does_not_govern(documents: Documents) -> None:
    """A group on another job leaves concurrent uploads possible."""
    publisher, _ = find_publisher(documents)
    other = {"runs-on": "ubuntu-latest", "steps": [{"run": "true"}]}
    other["concurrency"] = publisher.pop("concurrency")
    typ.cast("dict[str, object]", publisher["jobs"])["other"] = other
    _assert_reports(publisher_violations, documents, "needs a concurrency group")


def test_publisher_group_is_keyed_on_the_ref(documents: Documents) -> None:
    """A fixed group lets a branch dispatch displace main's pending push."""
    publisher, _ = find_publisher(documents)
    publisher["concurrency"] = {"group": "coverage-main", "cancel-in-progress": False}
    _assert_reports(publisher_violations, documents, "keyed on github.ref")


@pytest.mark.parametrize("scope", ["upload step", "upload job"])
def test_upload_cannot_fail_green(documents: Documents, scope: str) -> None:
    """`continue-on-error` hides every failed upload behind a green run."""
    publisher, upload = find_publisher(documents)
    target = upload if scope == "upload step" else first_job(publisher)
    target["continue-on-error"] = True
    _assert_reports(publisher_violations, documents, "must not continue on error")


@pytest.mark.parametrize("lane", ["pull request step", "pull request job", "trunk"])
def test_coverage_cannot_fail_green(documents: Documents, lane: str) -> None:
    """A ratchet failure that turns green no longer holds the lane."""
    publisher, _ = find_publisher(documents)
    targets = {
        "pull request step": coverage_step(documents[LANE]),
        "pull request job": first_job(documents[LANE]),
        "trunk": coverage_step(publisher),
    }
    targets[lane]["continue-on-error"] = True
    _assert_reports(coverage_violations, documents, "must not continue on error")


def test_publisher_job_cannot_cancel(documents: Documents) -> None:
    """A job-level concurrency block cancels just as well."""
    publisher, _ = find_publisher(documents)
    first_job(publisher)["concurrency"] = {"group": "g", "cancel-in-progress": True}
    _assert_reports(publisher_violations, documents, "cancels")


@pytest.mark.parametrize(
    ("on", "expected"),
    [
        ({"push": {"branches": ["main"]}, "pull_request": None}, "must not answer"),
        ({"push": {"branches": ["**"]}}, "must answer exactly"),
        ({"push": {"tags": ["v*"]}}, "must answer exactly"),
        ({"workflow_dispatch": None}, "must answer exactly"),
    ],
)
def test_publisher_answers_only_a_push_to_main(
    documents: Documents, on: object, expected: str
) -> None:
    """Only main's pushes may write the baseline CodeScene is given."""
    publisher, _ = find_publisher(documents)
    publisher[True] = on
    _assert_reports(publisher_violations, documents, expected)


def test_publisher_job_runs_unconditionally(documents: Documents) -> None:
    """A job-level `if: false` skips the upload with every step intact."""
    publisher, _ = find_publisher(documents)
    first_job(publisher)["if"] = "false"
    _assert_reports(publisher_violations, documents, "must run unconditionally")


def test_token_binding_is_asserted_positively(documents: Documents) -> None:
    """Deleting the binding makes the guard false, and the upload skips forever."""
    _, upload = find_publisher(documents)
    del upload["env"]
    _assert_reports(publisher_violations, documents, "must bind")


def test_token_binding_cannot_move_to_the_job(documents: Documents) -> None:
    """A binding in a wider scope reaches every step of the job."""
    publisher, upload = find_publisher(documents)
    upload["env"] = {}
    first_job(publisher)["env"] = {"CS_ACCESS_TOKEN": CREDENTIAL_REFERENCE}
    _assert_reports(publisher_violations, documents, "must bind")
    _assert_reports(publisher_violations, documents, "outside the upload step")


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
    _assert_reports(publisher_violations, documents, expected)


def test_second_uploader_is_refused(documents: Documents) -> None:
    """A second uploader could publish where nothing here looks."""
    publisher, upload = find_publisher(documents)
    job_steps(publisher).append(copy.deepcopy(upload))
    found = publisher_violations(documents)
    assert found == ["expected one CodeScene upload step, found 2"], found


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
    _assert_reports(coverage_violations, documents, expected)


def test_pull_request_coverage_cannot_be_switched_off(documents: Documents) -> None:
    """`if: false` keeps the step while the ratchet never runs."""
    coverage_step(documents[LANE])["if"] = "false"
    _assert_reports(coverage_violations, documents, "may run only as")


def test_pull_request_coverage_needs_its_guard(documents: Documents) -> None:
    """Without its guard the step also runs on main's push, a second writer."""
    del coverage_step(documents[LANE])["if"]
    _assert_reports(coverage_violations, documents, "may run only as")


def test_pull_request_coverage_must_exist(documents: Documents) -> None:
    """Deleting the PR coverage step deletes the ratchet."""
    steps = job_steps(documents[LANE])
    steps.remove(coverage_step(documents[LANE]))
    _assert_reports(
        coverage_violations,
        documents,
        "no pull-request lane generates coverage for the ratchet",
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
    _assert_reports(coverage_violations, documents, expected)


@pytest.mark.parametrize("lane", ["publisher", "pull request"])
def test_only_mains_push_writes_the_baseline(documents: Documents, lane: str) -> None:
    """`publish-baseline: always` lets a branch write the ratchet baseline."""
    document = find_publisher(documents)[0] if lane == "publisher" else documents[LANE]
    inputs = typ.cast("dict[str, object]", coverage_step(document)["with"])
    inputs["publish-baseline"] = "always"
    _assert_reports(coverage_violations, documents, "publish-baseline")


def test_push_callee_cannot_write_a_second_baseline(documents: Documents) -> None:
    """A push workflow's local callee runs on the push, so its coverage counts."""
    publisher, _ = find_publisher(documents)
    step = copy.deepcopy(coverage_step(publisher))
    documents["cov.yml"] = {
        True: {"workflow_call": None},
        "jobs": {"c": {"steps": [step]}},
    }
    documents["caller.yml"] = {
        True: "push",
        "jobs": {"call": {"uses": "./.github/workflows/cov.yml"}},
    }
    _assert_reports(
        coverage_violations,
        documents,
        "cov.yml coverage can run on a push; guard it to pull requests",
    )


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
    found = retired_names(documents)
    assert found != [], "the retired checksum machinery went unreported"
