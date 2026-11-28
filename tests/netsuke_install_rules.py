"""Hold every lane that runs the test suite to a pinned Netsuke install.

`tests/test_driver.py` exercises the released Netsuke CLI and fails when no
`netsuke` is on `PATH`. So each job that runs the suite, whether through the
shared generate-coverage action or a direct `pytest` or `make test`, must
install a released Netsuke first, in a step nothing can skip. The install must
pin one exact version, and every lane must pin the same one, so the pull
request lane tests what the publisher measures. Which release that is lives in
the workflows alone: the rule names no version, so upgrading Netsuke is a
workflow edit, not a test edit. A job that calls a reusable workflow runs its
steps elsewhere and is outside this rule.
"""

from __future__ import annotations

import itertools
import re
import shlex
import typing as typ

from workflow_reader import Document, Step, calls, continues_on_error, jobs

COVERAGE_ACTION: typ.Final[str] = (
    "leynos/shared-actions/.github/actions/generate-coverage"
)
INSTALL_CRATE: typ.Final[str] = "netsuke-build"
#: The canonical spelling, used in messages. The rule itself reads the
#: command's words rather than comparing this text.
INSTALL_COMMAND: typ.Final[str] = (
    f"cargo binstall --no-confirm --locked --version <x.y.z> {INSTALL_CRATE}"
)
# Explicit `[0-9]` rather than `\d`: Python's `\d` matches every Unicode
# decimal digit, but SemVer identifiers are ASCII only.
_NUMERIC: typ.Final[str] = r"(?:0|[1-9][0-9]*)"
_PRERELEASE_PART: typ.Final[str] = rf"(?:{_NUMERIC}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
#: The SemVer 2.0.0 grammar: ASCII only, no leading zeroes in numeric
#: components and no empty prerelease or build identifiers. Shared with the
#: CLI contract test so both read "a Netsuke release" the same way.
SEMVER: typ.Final[str] = (
    rf"{_NUMERIC}\.{_NUMERIC}\.{_NUMERIC}"
    rf"(?:-{_PRERELEASE_PART}(?:\.{_PRERELEASE_PART})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
#: One exact release, optionally with cargo's single `=` operator. Ranges such
#: as `0.1` or `^0.1.0` would let a lane drift to a release no other lane
#: installs, so they are not pins.
EXACT_VERSION: typ.Final[re.Pattern[str]] = re.compile(rf"=?{SEMVER}")
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


def install_command(version: str) -> str:
    """Return the canonical install command for ``version``.

    Examples
    --------
    >>> install_command("1.2.3")
    'cargo binstall --no-confirm --locked --version 1.2.3 netsuke-build'

    """
    return INSTALL_COMMAND.replace("<x.y.z>", version)


def _requested_versions(words: list[str]) -> list[str]:
    """Return every version the words request, in any binstall spelling."""
    attached = [
        word.removeprefix(f"{INSTALL_CRATE}@")
        for word in words
        if word.startswith(f"{INSTALL_CRATE}@")
    ]
    inline = [
        word.removeprefix("--version=")
        for word in words
        if word.startswith("--version=")
    ]
    separate = [
        following
        for word, following in itertools.pairwise(words)
        if word == "--version"
    ]
    return attached + inline + separate


def _pinned_version(words: list[str]) -> str | None:
    """Return the exact version the words pin, or ``None`` when there is none.

    Two different requests in one command are ambiguous, so they pin nothing.
    """
    requested = _requested_versions(words)
    # Validate the request as written before normalizing it, so `==1.2.3`
    # cannot become the valid `1.2.3` by losing both operators.
    if not all(EXACT_VERSION.fullmatch(version) for version in requested):
        return None
    pinned = {version.removeprefix("=") for version in requested}
    if len(pinned) != 1:
        return None
    (version,) = pinned
    return version


def _names_crate(words: list[str]) -> bool:
    """Return whether the words name the crate, bare or with an attached version."""
    return any(
        word == INSTALL_CRATE or word.startswith(f"{INSTALL_CRATE}@") for word in words
    )


def _segments(line: str) -> list[tuple[str, list[str]]]:
    """Split a line at shell operators into (preceding operator, words) pairs."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[tuple[str, list[str]]] = []
    operator = ""
    words: list[str] = []
    for token in tokens:
        if token and set(token) <= set("&|;()<>"):
            segments.append((operator, words))
            operator, words = token, []
        else:
            words.append(token)
    segments.append((operator, words))
    return segments


def _line_installed_version(line: str) -> str | None:
    """Return the version a line's `cargo binstall --locked` pins, if any.

    Only the install command's own words count, so a later `echo --locked`
    does not satisfy it, and the command must be one whose failure fails the
    step: nothing but `&&` may follow it (`|| true`, `;` and pipes hide it).
    """
    segments = _segments(line)
    for index, (operator, raw) in enumerate(segments):
        words = _command(shlex.join(raw))
        if words[:2] != ["cargo", "binstall"] or operator not in {"", "&&"}:
            continue
        following = segments[index + 1][0] if index + 1 < len(segments) else ""
        if following not in {"", "&&"}:
            continue
        if "--locked" not in words or not _names_crate(words):
            continue
        version = _pinned_version(words)
        if version is not None:
            return version
    return None


def installed_version(step: Step) -> str | None:
    """Return the Netsuke version a step installs, or ``None``.

    What matters is that the command is a `cargo binstall` of the crate at one
    exact version with `--locked` (so the build resolves the versions the
    release was tested with), not the exact text, so option order and extra
    options do not matter. The command must be a line of its own, so a comment
    or an echo that merely mentions it does not count.

    Examples
    --------
    >>> installed_version({"run": install_command("1.2.3")})
    '1.2.3'
    >>> reordered = "cargo binstall --locked netsuke-build --version 1.2.3-rc.1"
    >>> installed_version({"run": reordered})
    '1.2.3-rc.1'
    >>> installed_version({"run": "cargo binstall --locked netsuke-build"}) is None
    True
    >>> unlocked = install_command("1.2.3").replace("--locked ", "")
    >>> installed_version({"run": unlocked}) is None
    True

    """
    for line in str(step.get("run", "")).splitlines():
        version = _line_installed_version(line)
        if version is not None:
            return version
    return None


def installs_netsuke(step: Step) -> bool:
    """Return whether a step installs a pinned Netsuke release."""
    return installed_version(step) is not None


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


def _job_version(job_steps: list[Step]) -> str | None:
    """Return the version a job's first pinned Netsuke install step installs."""
    versions = (installed_version(step) for step in job_steps)
    return next((version for version in versions if version is not None), None)


def _job_violations(where: str, job_steps: list[Step]) -> list[str]:
    """Check one suite job's install step against the rule."""
    first_suite = next(i for i, step in enumerate(job_steps) if runs_suite(step))
    installs = [i for i, step in enumerate(job_steps) if installs_netsuke(step)]
    if not installs:
        return [f"{where}: runs the suite but never installs `{INSTALL_COMMAND}`"]
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


def _version_disagreement(pins: dict[str, str]) -> list[str]:
    """Report suite lanes that pin different Netsuke releases."""
    if len(set(pins.values())) <= 1:
        return []
    listed = ", ".join(f"{where}={version}" for where, version in sorted(pins.items()))
    return [f"suite lanes install different Netsuke releases: {listed}"]


def netsuke_install_violations(documents: dict[str, Document]) -> list[str]:
    """Report suite jobs that do not install one shared, pinned Netsuke.

    A reading with no suite job at all is itself a violation: the rule is a
    refusal, and a refusal over nothing would pass any repository.
    """
    found = suite_jobs(documents)
    if not found:
        return ["no workflow job runs the test suite"]
    problems: list[str] = []
    pins: dict[str, str] = {}
    for name, job_id in found:
        where = f"{name}:{job_id}"
        job_steps = _steps_of(jobs(name, documents[name])[job_id])
        problems.extend(_job_violations(where, job_steps))
        version = _job_version(job_steps)
        if version is not None:
            pins[where] = version
    return problems + _version_disagreement(pins)
