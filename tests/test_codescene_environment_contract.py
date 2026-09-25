"""Prove the `codescene` environment sits on the uploading job and nowhere else.

Each test mutates a copy of this repository's workflows the way a later edit
could, and asserts the clause meant to catch it does. The check step, the ref
guard and `access-token:` stay held by the publisher and token contracts.
"""

from __future__ import annotations

import typing as typ

import pytest
from codescene_contract_support import (
    LANE,
    SKIP_REASON,
    WORKFLOWS,
    Documents,
    assert_clean,
    assert_reports,
    find_publisher,
    first_job,
    job_steps,
)
from codescene_environment_rules import (
    MISSING,
    REACHABLE,
    STRAY,
    environment_violations,
)

pytestmark = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason=SKIP_REASON)


def test_repository_places_the_environment(documents: Documents) -> None:
    """The publisher declares the environment and nothing else does."""
    assert_clean(environment_violations, documents)


def test_publisher_cannot_drop_the_environment(documents: Documents) -> None:
    """Without it the moved token never reaches the upload, which then skips."""
    publisher, _ = find_publisher(documents)
    del first_job(publisher)["environment"]
    assert_reports(environment_violations, documents, MISSING)


def test_publisher_cannot_name_another_environment(documents: Documents) -> None:
    """Another environment holds no CodeScene token."""
    publisher, _ = find_publisher(documents)
    first_job(publisher)["environment"] = "production"
    assert_reports(environment_violations, documents, MISSING)


def test_mapping_form_is_accepted(documents: Documents) -> None:
    """`{name: codescene}` is the same declaration as the bare string."""
    publisher, _ = find_publisher(documents)
    first_job(publisher)["environment"] = {"name": "codescene"}
    assert_clean(environment_violations, documents)


def test_no_other_job_may_declare_it(documents: Documents) -> None:
    """A second holder of the token widens what can read it."""
    publisher, _ = find_publisher(documents)
    typ.cast("dict[str, object]", publisher["jobs"])["other"] = {
        "runs-on": "ubuntu-latest",
        "environment": "codescene",
        "steps": [{"run": "true"}],
    }
    assert_reports(environment_violations, documents, STRAY)


def test_no_pull_request_job_may_declare_it(documents: Documents) -> None:
    """A pull request's own code must never be able to request the token."""
    first_job(documents[LANE])["environment"] = {"name": "codescene"}
    assert_reports(environment_violations, documents, REACHABLE)


def test_an_empty_reading_is_refused(documents: Documents) -> None:
    """With no uploader left the rule says so rather than passing."""
    publisher, upload = find_publisher(documents)
    job_steps(publisher).remove(upload)
    assert_reports(environment_violations, documents, "no workflow job calls")
