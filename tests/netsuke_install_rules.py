"""Hold every lane that runs the test suite to the released Netsuke install.

`tests/test_driver.py` exercises the released Netsuke CLI and fails when no
`netsuke` is on `PATH`. So each job that runs the suite, whether through the
shared generate-coverage action or a direct `pytest` or `make test`, must
install that release first, at the version pinned here, in a step nothing can
skip. A job that calls a reusable workflow runs its steps elsewhere and is
outside this rule.
"""

from __future__ import annotations

import shlex
import typing as typ

from codescene_workflow_reader import Document, Step, calls, continues_on_error, jobs

COVERAGE_ACTION: typ.Final[str] = (
    "leynos/shared-actions/.github/actions/generate-coverage"
)
#: The released Netsuke the driver tests target. Named here rather than read
#: from ci.yml, so changing every lane at once still fails until this changes.
NETSUKE_VERSION: typ.Final[str] = "0.1.0-beta1"
INSTALL_COMMAND: typ.Final[str] = (
    f"cargo binstall --no-confirm --version {NETSUKE_VERSION} netsuke-build"
)
#: Launchers that run the command after them, so `uv run pytest` and
#: `python -m pytest` are read as the `pytest` they start.
LAUNCHERS: typ.Final[tuple[tuple[str, ...], ...]] = (
    ("uv", "run"),
    ("python", "-m"),
    ("python3", "-m"),
)
#: The Make goal that runs the suite.
SUITE_GOAL: typ.Final[str] = "test"


def _command(line: str) -> list[str]:
    """Return a shell line's words with any launcher prefix removed."""
    try:
        words = shlex.split(line, comments=True)
    except ValueError:
        return []
    for launcher in LAUNCHERS:
        if tuple(words[: len(launcher)]) == launcher:
            return words[len(launcher) :]
    return words


def _line_runs_suite(line: str) -> bool:
    """Return whether one command runs pytest, or `make` with the suite goal.

    The command word decides, so `echo 'make test'` names the suite without
    running it, and options such as `make -j2 test` do not hide the goal.
    """
    words = _command(line)
    if not words:
        return False
    program = words[0].rsplit("/", 1)[-1]
    if program == "pytest":
        return True
    goals = [word for word in words[1:] if not word.startswith("-") and "=" not in word]
    return program == "make" and SUITE_GOAL in goals


def runs_suite(step: Step) -> bool:
    """Return whether a step runs the test suite.

    Examples
    --------
    >>> runs_suite({"run": "make test"})
    True
    >>> runs_suite({"run": "make lint"})
    False
    >>> runs_suite({"run": "echo 'make test'"})
    False

    """
    if calls(step, COVERAGE_ACTION):
        return True
    script = str(step.get("run", ""))
    return any(_line_runs_suite(line) for line in script.splitlines())


def installs_netsuke(step: Step) -> bool:
    """Return whether a step installs the pinned Netsuke release.

    The command must be a line of its own, so a comment or an echo that
    merely mentions it does not count.

    Examples
    --------
    >>> installs_netsuke({"run": INSTALL_COMMAND})
    True
    >>> installs_netsuke({"run": "cargo binstall netsuke-build"})
    False

    """
    lines = str(step.get("run", "")).splitlines()
    return any(line.strip() == INSTALL_COMMAND for line in lines)


def _steps_of(job: dict[str, object]) -> list[Step]:
    """Return a job's steps, or none for a job that calls a reusable workflow."""
    return typ.cast("list[Step]", job.get("steps", []))


def suite_jobs(documents: dict[str, Document]) -> list[tuple[str, str]]:
    """Return (workflow, job id) for every job with a step running the suite."""
    return [
        (name, job_id)
        for name, document in sorted(documents.items())
        for job_id, job in jobs(name, document).items()
        if any(runs_suite(step) for step in _steps_of(job))
    ]


def _job_violations(where: str, job_steps: list[Step]) -> list[str]:
    """Check one suite job's install step against the rule."""
    first_suite = next(i for i, step in enumerate(job_steps) if runs_suite(step))
    installs = [i for i, step in enumerate(job_steps) if installs_netsuke(step)]
    if not installs:
        return [f"{where}: runs the suite but never runs `{INSTALL_COMMAND}`"]
    install = installs[0]
    problems = []
    if install > first_suite:
        problems.append(f"{where}: installs Netsuke only after the suite runs")
    step = job_steps[install]
    if "if" in step:
        problems.append(f"{where}: the Netsuke install must not carry an `if:`")
    if continues_on_error(step):
        problems.append(f"{where}: the Netsuke install must not continue on error")
    return problems


def netsuke_install_violations(documents: dict[str, Document]) -> list[str]:
    """Report every suite job that does not install the pinned Netsuke.

    A reading with no suite job at all is itself a violation: the rule is a
    refusal, and a refusal over nothing would pass any repository.
    """
    found = suite_jobs(documents)
    if not found:
        return ["no workflow job runs the test suite"]
    problems = []
    for name, job_id in found:
        job = jobs(name, documents[name])[job_id]
        problems.extend(_job_violations(f"{name}:{job_id}", _steps_of(job)))
    return problems
