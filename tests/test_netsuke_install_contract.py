"""Prove every suite lane installs the released Netsuke before its tests run.

Each test mutates a copy of this repository's workflows the way a later edit
could, and asserts that the rule reports it; the clean and narrow cases show
the rule passes this repository and ignores jobs that never run the suite.
"""

from __future__ import annotations

import typing as typ

import pytest
from codescene_contract_support import (
    SKIP_REASON,
    WORKFLOWS,
    Documents,
    assert_clean,
    assert_reports,
    job_steps,
    lane_jobs,
)
from netsuke_install_rules import (
    INSTALL_COMMAND,
    installs_netsuke,
    netsuke_install_violations,
    suite_jobs,
)

if typ.TYPE_CHECKING:
    from codescene_workflow_reader import Step

pytestmark = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason=SKIP_REASON)

PUBLISHER: typ.Final[str] = "coverage-main.yml"


def _install_step(documents: Documents, name: str) -> Step:
    """Return a workflow's Netsuke install step."""
    return next(s for s in job_steps(documents[name]) if installs_netsuke(s))


def test_repository_installs_netsuke_in_every_suite_lane(documents: Documents) -> None:
    """Both the pull-request lane and the publisher install Netsuke first."""
    assert_clean(netsuke_install_violations, documents)


def test_both_coverage_lanes_are_recognised(documents: Documents) -> None:
    """The rule sees the lanes it guards, so an empty reading cannot pass."""
    workflows = {name for name, _ in suite_jobs(documents)}
    assert {"ci.yml", PUBLISHER} <= workflows


def test_publisher_cannot_drop_the_install(documents: Documents) -> None:
    """Without the install the driver contract test fails on main."""
    steps = job_steps(documents[PUBLISHER])
    steps.remove(_install_step(documents, PUBLISHER))
    assert_reports(netsuke_install_violations, documents, "never runs")


def test_publisher_cannot_install_another_version(documents: Documents) -> None:
    """The driver tests target one release; another version is not it."""
    _install_step(documents, PUBLISHER)["run"] = INSTALL_COMMAND.replace(
        "0.1.0-beta1", "0.1.0"
    )
    assert_reports(netsuke_install_violations, documents, "never runs")


def test_every_lane_moving_together_still_fails(documents: Documents) -> None:
    """Changing both lanes at once cannot move the pinned release unseen."""
    for name in ("ci.yml", PUBLISHER):
        _install_step(documents, name)["run"] = INSTALL_COMMAND.replace(
            "0.1.0-beta1", "0.2.0"
        )
    assert_reports(netsuke_install_violations, documents, "ci.yml:lint-test")


def test_publisher_cannot_install_after_the_suite(documents: Documents) -> None:
    """An install after generate-coverage leaves the tests without Netsuke."""
    steps = job_steps(documents[PUBLISHER])
    install = _install_step(documents, PUBLISHER)
    steps.remove(install)
    steps.append(install)
    assert_reports(netsuke_install_violations, documents, "only after the suite")


def test_install_cannot_be_guarded(documents: Documents) -> None:
    """A guard could skip the install while the suite still runs."""
    _install_step(documents, PUBLISHER)["if"] = "github.event_name == 'pull_request'"
    assert_reports(netsuke_install_violations, documents, "must not carry an `if:`")


def test_install_cannot_continue_on_error(documents: Documents) -> None:
    """A failed install would otherwise surface only as a test failure."""
    _install_step(documents, PUBLISHER)["continue-on-error"] = True
    assert_reports(netsuke_install_violations, documents, "must not continue on error")


def test_a_new_pytest_lane_needs_the_install(documents: Documents) -> None:
    """A job running the suite directly is held to the rule too."""
    lane_jobs(documents)["tests"] = {
        "runs-on": "ubuntu-latest",
        "steps": [{"run": "make test"}],
    }
    assert_reports(netsuke_install_violations, documents, "ci.yml:tests")


def test_a_lane_without_the_suite_needs_no_install(documents: Documents) -> None:
    """The rule is narrow: a job that never runs the tests is not held to it."""
    lane_jobs(documents)["spelling"] = {
        "runs-on": "ubuntu-latest",
        "steps": [{"run": "make spelling"}],
    }
    assert_clean(netsuke_install_violations, documents)


def test_a_mention_is_not_an_install(documents: Documents) -> None:
    """Echoing the command does not install anything."""
    _install_step(documents, PUBLISHER)["run"] = f"echo '{INSTALL_COMMAND}'"
    assert_reports(netsuke_install_violations, documents, "never runs")


def test_an_empty_reading_is_refused(documents: Documents) -> None:
    """With no suite lane left the rule reports it rather than passing."""
    for name in ("ci.yml", PUBLISHER):
        steps = job_steps(documents[name])
        steps[:] = [s for s in steps if "generate-coverage" not in str(s.get("uses"))]
    assert_reports(netsuke_install_violations, documents, "no workflow job runs")
