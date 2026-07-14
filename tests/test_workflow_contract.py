"""Contract tests for the mutation-testing caller workflow.

The executable logic lives in the ``leynos/shared-actions`` reusable
workflow, which carries its own unit and integration tests; this
repository's caller is declarative configuration. These tests parse the
caller with PyYAML and assert the contract it must uphold, so drift
(repointing the reference at a branch, widening permissions, or losing
the mutmut configuration) fails CI on the pull request rather than
surfacing in a scheduled or manual run. The caller must reference the
correct reusable workflow at a commit SHA; Dependabot owns the SHA
value, so these tests assert the shape of the pin, not which commit it
names.
"""

from __future__ import annotations

import re
import typing as typ
from pathlib import Path

import pytest
import yaml

WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "mutation-testing.yml"
)

pytestmark = pytest.mark.skipif(
    not WORKFLOW_PATH.exists(),
    reason="workflow file not present in this working copy (e.g. "
    "inside mutmut's mutants/ sandbox, which does not copy .github/)",
)

#: The reusable workflow the caller must reference, pinned to a full
#: 40-hex commit SHA. Dependabot owns the SHA value; this test does
#: not pin it.
USES_RE = re.compile(
    r"^leynos/shared-actions/\.github/workflows/mutation-mutmut\.yml@[0-9a-f]{40}$"
)

#: The caller inputs this repository relies on; anything else must use
#: the reusable workflow's defaults.
EXPECTED_WITH = {
    "paths": "post_turn_quality_stop_hook/",
    "module-prefix-strip": "",
    "python-version": "3.14",
}


def _load() -> dict[str, object]:
    """Parse the workflow file."""
    return typ.cast(
        "dict[str, object]",
        yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8")),
    )


def _triggers(workflow: dict[str, object]) -> dict[str, object]:
    """Return the ``on:`` mapping (PyYAML parses the bare key as True)."""
    raw = typ.cast("dict[object, object]", workflow)
    triggers = raw.get("on", raw.get(True))
    assert isinstance(triggers, dict), "the workflow must declare an on: mapping"
    return typ.cast("dict[str, object]", triggers)


def _mutation_job(workflow: dict[str, object]) -> dict[str, object]:
    """Return the single calling job."""
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "the workflow must declare a jobs mapping"
    jobs_map = typ.cast("dict[str, object]", jobs)
    assert jobs_map, "the workflow must declare at least one job"
    assert list(jobs_map) == ["mutation"], (
        f"expected a single job named 'mutation', found {sorted(jobs_map)}"
    )
    return typ.cast("dict[str, object]", jobs_map["mutation"])


def test_uses_reference_is_pinned_to_a_full_sha() -> None:
    """The job must call mutation-mutmut.yml pinned to a full commit SHA."""
    uses = _mutation_job(_load()).get("uses")
    assert uses is not None, "jobs.mutation.uses is missing"
    assert isinstance(uses, str), f"jobs.mutation.uses must be a string, got {uses!r}"
    assert USES_RE.match(uses), (
        f"jobs.mutation.uses must reference mutation-mutmut.yml pinned to a "
        f"full 40-character lowercase-hex commit SHA, not a branch or tag: "
        f"{uses!r}"
    )


def test_job_permissions_are_exactly_least_privilege() -> None:
    """The job grants contents: read and id-token: write, nothing broader."""
    permissions = _mutation_job(_load()).get("permissions")
    assert permissions == {"contents": "read", "id-token": "write"}, (
        "jobs.mutation.permissions must be exactly "
        f"{{'contents': 'read', 'id-token': 'write'}}, got {permissions!r}"
    )


def test_workflow_default_permissions_are_empty() -> None:
    """The workflow-level default token scope is empty."""
    workflow = _load()
    assert workflow.get("permissions") == {}, (
        f"top-level permissions must be an empty mapping, got "
        f"{workflow.get('permissions')!r}"
    )


def test_concurrency_serializes_per_ref_without_cancelling() -> None:
    """Runs queue per ref instead of cancelling one another."""
    concurrency = _load().get("concurrency")
    assert isinstance(concurrency, dict), "the workflow must declare concurrency"
    concurrency_map = typ.cast("dict[str, object]", concurrency)
    assert concurrency_map.get("group") == "mutation-testing-${{ github.ref }}", (
        f"concurrency.group must key on the triggering ref, got "
        f"{concurrency_map.get('group')!r}"
    )
    assert concurrency_map.get("cancel-in-progress") is False, (
        f"concurrency.cancel-in-progress must be false, got "
        f"{concurrency_map.get('cancel-in-progress')!r}"
    )


def test_triggers_keep_schedule_and_plain_dispatch() -> None:
    """The daily schedule stays; dispatch declares no inputs."""
    triggers = _triggers(_load())
    schedule = triggers.get("schedule")
    assert schedule == [{"cron": "20 12 * * *"}], (
        f"on.schedule must be the daily 12:20 UTC cron, got {schedule!r}"
    )
    assert "workflow_dispatch" in triggers, "on.workflow_dispatch is missing"
    dispatch = triggers.get("workflow_dispatch") or {}
    inputs = typ.cast("dict[str, object]", dispatch).get("inputs") or {}
    assert not inputs, (
        "on.workflow_dispatch must not declare inputs; the Actions "
        "run-workflow control selects the ref"
    )


def test_with_block_carries_exactly_the_caller_configuration() -> None:
    """The caller sets only the flat-layout inputs; defaults cover the rest."""
    with_block = _mutation_job(_load()).get("with")
    assert with_block == EXPECTED_WITH, (
        f"jobs.mutation.with must be exactly {EXPECTED_WITH!r}, got {with_block!r}"
    )
