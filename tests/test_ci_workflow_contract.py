"""Contract tests for the CI workflow's CodeScene credential boundary.

These tests parse ``.github/workflows/ci.yml`` and ensure ``lint-test`` exposes
only a non-secret enablement signal at job scope. The CodeScene token and CLI
checksum remain limited to the two guarded CodeScene steps. Run this focused
contract with ``uv run pytest -q tests/test_ci_workflow_contract.py`` or the
full suite with ``make test``.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

import pytest
import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

pytestmark = pytest.mark.skipif(
    not WORKFLOW_PATH.exists(),
    reason="workflow file not present in this working copy (e.g. "
    "inside mutmut's mutants/ sandbox, which does not copy .github/)",
)

CODESCENE_ENV = {
    "CS_ACCESS_TOKEN": "${{ secrets.CS_ACCESS_TOKEN }}",
    "CODESCENE_CLI_SHA256": "${{ vars.CODESCENE_CLI_SHA256 }}",
}
CODESCENE_ENABLEMENT_ENV = {
    "HAS_CODESCENE_ACCESS_TOKEN": "${{ secrets.CS_ACCESS_TOKEN != '' }}",
}
CODESCENE_STEP_NAMES = {
    "Install CodeScene Coverage CLI",
    "Upload coverage to CodeScene",
}
EXPECTED_CODESCENE_STEP_COUNT = 2
CODESCENE_STEP_CONDITION = "env.HAS_CODESCENE_ACCESS_TOKEN == 'true'"


def _lint_test_job() -> dict[str, object]:
    """Return the CI job that runs repository validation."""
    workflow = typ.cast(
        "dict[str, object]", yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    )
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "the workflow must declare jobs"
    lint_test = jobs.get("lint-test")
    assert isinstance(lint_test, dict), "the workflow must declare lint-test"
    return typ.cast("dict[str, object]", lint_test)


def test_codescene_credentials_are_scoped_to_codescene_steps() -> None:
    """Keep CodeScene credentials away from unrelated CI subprocesses."""
    lint_test = _lint_test_job()
    assert lint_test.get("env") == CODESCENE_ENABLEMENT_ENV, (
        "lint-test must expose only the non-secret CodeScene enablement signal"
    )

    steps = lint_test.get("steps")
    assert isinstance(steps, list), "lint-test must declare a steps list"
    codescene_steps = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("name") in CODESCENE_STEP_NAMES
    ]
    credential_steps = [
        step
        for step in steps
        if isinstance(step, dict)
        and isinstance(step_env := step.get("env"), dict)
        and ("CS_ACCESS_TOKEN" in step_env or "CODESCENE_CLI_SHA256" in step_env)
    ]
    assert len(codescene_steps) == EXPECTED_CODESCENE_STEP_COUNT, (
        "expected exactly two CodeScene steps"
    )
    assert credential_steps == codescene_steps, (
        "only the named CodeScene steps may receive CodeScene credentials"
    )
    for step in codescene_steps:
        assert step.get("if") == CODESCENE_STEP_CONDITION, (
            "CodeScene steps must use the non-secret job-level enablement signal"
        )
        assert step.get("env") == CODESCENE_ENV, (
            f"CodeScene step must receive only its required credentials, got {step!r}"
        )
