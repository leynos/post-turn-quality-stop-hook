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

from hypothesis import HealthCheck, settings

settings.register_profile(
    "mutmut",
    suppress_health_check=[HealthCheck.differing_executors],
    database=None,
    deadline=None,
    derandomize=True,
)

if "MUTANT_UNDER_TEST" in os.environ:
    settings.load_profile("mutmut")
