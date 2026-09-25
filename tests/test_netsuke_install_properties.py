"""Property tests for the Netsuke install rule over generated suite jobs.

Each case builds one job from independent choices: whether an install step is
present, whether its command is exact, where it sits relative to the suite,
and whether it carries a guard or `continue-on-error`. The expected verdict is
stated from those choices, not recomputed from the steps, so the rule is
checked against the intent it encodes.
"""

from __future__ import annotations

import typing as typ

from hypothesis import given
from hypothesis import strategies as st
from netsuke_install_rules import INSTALL_COMMAND, netsuke_install_violations

if typ.TYPE_CHECKING:
    from codescene_workflow_reader import Document, Step

SUITE_STEPS: tuple[Step, ...] = (
    {"uses": "leynos/shared-actions/.github/actions/generate-coverage@" + "0" * 40},
    {"run": "make test"},
    {"run": "make -j2 test"},
    {"run": "uv run pytest -q"},
)
FILLER_STEPS: tuple[Step, ...] = (
    {"run": "make lint"},
    {"run": "echo 'make test'"},
    {"uses": "actions/checkout@" + "1" * 40},
)
NEAR_MISSES = (
    "cargo binstall netsuke-build",
    f"echo '{INSTALL_COMMAND}'",
    INSTALL_COMMAND.replace("0.1.0-beta1", "0.1.0"),
)


@st.composite
def suite_jobs(draw: st.DrawFn) -> tuple[list[Step], bool]:
    """Draw a job's steps and whether the rule should accept it."""
    fillers = draw(st.lists(st.sampled_from(FILLER_STEPS), max_size=3))
    suite: Step = dict(draw(st.sampled_from(SUITE_STEPS)))
    has_install = draw(st.booleans())
    exact = draw(st.booleans())
    before = draw(st.booleans())
    guarded = draw(st.booleans())
    tolerant = draw(st.booleans())
    steps: list[Step] = [dict(step) for step in fillers]
    if not has_install:
        steps.append(suite)
        return steps, False
    command = INSTALL_COMMAND if exact else draw(st.sampled_from(NEAR_MISSES))
    install: Step = {"run": command}
    if guarded:
        install["if"] = "github.event_name == 'push'"
    if tolerant:
        install["continue-on-error"] = True
    steps.extend([install, suite] if before else [suite, install])
    return steps, exact and before and not guarded and not tolerant


@given(suite_jobs())
def test_the_rule_accepts_exactly_the_compliant_jobs(
    case: tuple[list[Step], bool],
) -> None:
    """A job passes only with an exact, unguarded, strict install before the suite."""
    steps, compliant = case
    documents = typ.cast(
        "dict[str, Document]", {"probe.yml": {"jobs": {"probe": {"steps": steps}}}}
    )
    found = netsuke_install_violations(documents)
    assert (not found) is compliant, (
        f"compliant={compliant} but the rule reported {found} for {steps}"
    )
