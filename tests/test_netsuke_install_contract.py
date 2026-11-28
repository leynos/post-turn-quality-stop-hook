"""Prove every suite lane installs one pinned Netsuke before its tests run.

Each test mutates a copy of this repository's workflows the way a later edit
could, and asserts that the rule reports it; the clean and narrow cases show
the rule passes this repository and ignores jobs that never run the suite.
No test names the release the workflows pin: each reads the repository's own
pin, or uses an arbitrary version, so upgrading Netsuke needs no test edit.
"""

from __future__ import annotations

import typing as typ

import pytest
from netsuke_install_rules import (
    install_command,
    installed_version,
    installs_netsuke,
    netsuke_install_violations,
    suite_jobs,
)
from workflow_support import (
    SKIP_REASON,
    WORKFLOWS,
    Documents,
    assert_clean,
    assert_reports,
    job_steps,
    lane_jobs,
)

if typ.TYPE_CHECKING:
    from workflow_reader import Step

pytestmark = pytest.mark.skipif(not WORKFLOWS.is_dir(), reason=SKIP_REASON)

PUBLISHER: typ.Final[str] = "coverage-main.yml"
#: A release no workflow pins, for moving one lane away from the others.
OTHER_VERSION: typ.Final[str] = "9.9.9"


def _install_step(documents: Documents, name: str) -> Step:
    """Return a workflow's Netsuke install step."""
    return next(s for s in job_steps(documents[name]) if installs_netsuke(s))


def _pin(documents: Documents) -> str:
    """Return the release the pull-request lane pins, whatever it is."""
    version = installed_version(_install_step(documents, "ci.yml"))
    assert version is not None
    return version


def _pinned_install(documents: Documents) -> str:
    """Return the canonical install command for the repository's own pin."""
    return install_command(_pin(documents))


def test_repository_installs_netsuke_in_every_suite_lane(documents: Documents) -> None:
    """Both the pull-request lane and the publisher install Netsuke first."""
    assert_clean(netsuke_install_violations, documents)


def test_both_coverage_lanes_are_recognised(documents: Documents) -> None:
    """The rule sees the lanes it guards, so an empty reading cannot pass."""
    workflows = {name for name, _ in suite_jobs(documents)}
    assert {"ci.yml", PUBLISHER} <= workflows, (
        f"both coverage lanes must be read as running the suite, got {workflows}"
    )


def test_publisher_cannot_drop_the_install(documents: Documents) -> None:
    """Without the install the driver contract test fails on main."""
    steps = job_steps(documents[PUBLISHER])
    steps.remove(_install_step(documents, PUBLISHER))
    assert_reports(netsuke_install_violations, documents, "never installs")


def test_lanes_cannot_pin_different_releases(documents: Documents) -> None:
    """The publisher must measure the release the pull-request lane tests."""
    _install_step(documents, PUBLISHER)["run"] = install_command(OTHER_VERSION)
    assert_reports(
        netsuke_install_violations, documents, "install different Netsuke releases"
    )


def test_every_lane_moving_together_is_an_upgrade(documents: Documents) -> None:
    """Moving every lane to one new release is how Netsuke is upgraded."""
    for name in ("ci.yml", PUBLISHER):
        _install_step(documents, name)["run"] = install_command(OTHER_VERSION)
    assert_clean(netsuke_install_violations, documents)


@pytest.mark.parametrize("requirement", ["^0.1.0", "0.1", "*", ">=0.1.0"])
def test_a_version_range_is_not_a_pin(documents: Documents, requirement: str) -> None:
    """A range lets one lane drift to a release no other lane installs."""
    _install_step(documents, PUBLISHER)["run"] = install_command(requirement)
    assert_reports(netsuke_install_violations, documents, "never installs")


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


@pytest.mark.parametrize(
    "command",
    ["make test", "make -j2 test", "uv run pytest -q", "python -m pytest tests"],
)
def test_a_new_pytest_lane_needs_the_install(
    documents: Documents, command: str
) -> None:
    """A job running the suite directly is held to the rule, however spelt."""
    lane_jobs(documents)["tests"] = {
        "runs-on": "ubuntu-latest",
        "steps": [{"run": command}],
    }
    assert_reports(netsuke_install_violations, documents, "ci.yml:tests")


def test_a_lane_without_the_suite_needs_no_install(documents: Documents) -> None:
    """The rule is narrow: a job that never runs the tests is not held to it."""
    lane_jobs(documents)["spelling"] = {
        "runs-on": "ubuntu-latest",
        "steps": [
            {"run": "make spelling"},
            {"run": "echo 'make test'"},
            {"run": "make test-report-only"},
        ],
    }
    assert_clean(netsuke_install_violations, documents)


def test_publisher_cannot_drop_locked(documents: Documents) -> None:
    """Without `--locked` the build may resolve crates the release never saw."""
    _install_step(documents, PUBLISHER)["run"] = _pinned_install(documents).replace(
        "--locked ", ""
    )
    assert_reports(netsuke_install_violations, documents, "never installs")


def test_option_order_and_spelling_do_not_matter(documents: Documents) -> None:
    """The rule reads what the command does, not its exact text."""
    pin = _pin(documents)
    _install_step(documents, PUBLISHER)["run"] = (
        f"cargo binstall netsuke-build --version={pin} --locked --no-confirm"
    )
    assert_clean(netsuke_install_violations, documents)


def test_attached_version_spelling_is_accepted(documents: Documents) -> None:
    """`crate@version` is a valid binstall spelling of the pin."""
    _install_step(documents, PUBLISHER)["run"] = (
        f"cargo binstall --no-confirm --locked netsuke-build@{_pin(documents)}"
    )
    assert_clean(netsuke_install_violations, documents)


@pytest.mark.parametrize(
    "template",
    [
        (
            "cargo binstall --no-confirm --version {version} netsuke-build"
            " && echo --locked"
        ),
        "{install} || true",
        "{install} | tee install.log",
        "{install}; true",
    ],
)
def test_install_must_be_one_whose_failure_fails_the_step(
    documents: Documents, template: str
) -> None:
    """Words after an operator, or a masked exit status, are not an install."""
    install = _pinned_install(documents)
    command = template.format(version=_pin(documents), install=install)
    _install_step(documents, PUBLISHER)["run"] = command
    assert_reports(netsuke_install_violations, documents, "never installs")


def test_two_versions_in_one_command_are_not_a_pin(documents: Documents) -> None:
    """An install naming two releases pins neither."""
    _install_step(documents, PUBLISHER)["run"] = (
        f"{_pinned_install(documents)} --version={OTHER_VERSION}"
    )
    assert_reports(netsuke_install_violations, documents, "never installs")


def test_a_chained_install_that_still_fails_the_step_is_accepted(
    documents: Documents,
) -> None:
    """`install && next` keeps the install's exit status."""
    _install_step(documents, PUBLISHER)["run"] = (
        f"{_pinned_install(documents)} && netsuke --help"
    )
    assert_clean(netsuke_install_violations, documents)


def test_a_mention_is_not_an_install(documents: Documents) -> None:
    """Echoing the command does not install anything."""
    _install_step(documents, PUBLISHER)["run"] = f"echo '{_pinned_install(documents)}'"
    assert_reports(netsuke_install_violations, documents, "never installs")


def test_an_empty_reading_is_refused(documents: Documents) -> None:
    """With no suite lane left the rule reports it rather than passing."""
    for name in ("ci.yml", PUBLISHER):
        steps = job_steps(documents[name])
        steps[:] = [s for s in steps if "generate-coverage" not in str(s.get("uses"))]
    assert_reports(netsuke_install_violations, documents, "no workflow job runs")
