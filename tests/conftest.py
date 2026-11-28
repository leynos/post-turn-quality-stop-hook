"""Shared pytest configuration.

Registers a Hypothesis profile for mutation-testing runs. mutmut drives
pytest in-process several times per session (stats collection, a clean
run, then one run per mutant), so Hypothesis observes class-based tests
executed by differing executors and fails its ``differing_executors``
health check. That is inherent to mutmut's runner rather than a defect
in the tests, so the profile suppresses the check — and disables the
example database and deadline — for mutation runs only. mutmut exports
``MUTANT_UNDER_TEST`` in every phase (empty for the clean run), which is
how the profile is selected.
"""

from __future__ import annotations

import os
import types

import pytest
from hypothesis import HealthCheck, settings
from workflow_support import Documents, fresh_documents

from post_turn_quality_stop_hook import disk_space as disk_space_mod

#: Free space the default fixture reports: one tebibyte, far above any sane
#: threshold.
TESTING_FREE_BYTES = 1024**4

settings.register_profile(
    "mutmut",
    suppress_health_check=[HealthCheck.differing_executors],
    database=None,
    deadline=None,
    derandomize=True,
)

if "MUTANT_UNDER_TEST" in os.environ:
    settings.load_profile("mutmut")


@pytest.fixture
def documents() -> Documents:
    """Give each contract test its own copy of the workflows to mutate."""
    return fresh_documents()


@pytest.fixture(autouse=True)
def _ample_free_space(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report an ample volume so the free-space probe never short-circuits.

    Tests that exercise the probe override this fixture. Without the default,
    every hook invocation in the suite would measure the volume the suite
    happens to run on, so a nearly full test machine would change what the
    tests exercise.
    """

    def ample(_path: object) -> types.SimpleNamespace:
        """Report the fixture's free space for every probed path."""
        return types.SimpleNamespace(
            total=TESTING_FREE_BYTES, used=0, free=TESTING_FREE_BYTES
        )

    monkeypatch.setattr(disk_space_mod.shutil, "disk_usage", ample)
