"""Contract tests for the Skylos dead-code gate in Make and CI.

Skylos accepts scan options before a scan path, but its standalone
``whitelist`` subcommand must appear immediately after ``skylos``. The scanner
also parses source with its own Python AST, so the Makefile must run it with
Python 3.14. Makeutil exposes those Makefile rules as structured data, avoiding
fragile assertions about whitespace or nearby source text.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess  # noqa: S404 - contract tests invoke fixed local commands.
import tomllib
import typing as typ
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"

_MAKEUTIL_COMMAND: typ.Final = ("makeutil", "parse", "Makefile")
_MAKEUTIL_REVISION: typ.Final = "29fc5a1634ffbaa18a773eed9dff1b2838a45d9c"
_MAKEUTIL_TOOLCHAIN: typ.Final = "nightly-2026-05-28"
_MISSING_ARGUMENT_EXIT_CODE: typ.Final = 2
_MAKEUTIL_INSTALL_TOKENS: typ.Final = (
    "rustup",
    "toolchain",
    "install",
    "${MAKEUTIL_TOOLCHAIN}",
    "--profile",
    "minimal",
    "RUSTFLAGS=-Zpolonius=next",
    "cargo",
    "+${MAKEUTIL_TOOLCHAIN}",
    "install",
    "--git",
    "https://github.com/leynos/makeutil",
    "--rev",
    "${MAKEUTIL_REVISION}",
    "--locked",
    "--force",
    "makeutil",
)


def _mapping(value: object, *, subject: str) -> dict[str, object]:
    """Return a JSON object, naming the unexpected subject on failure."""
    assert isinstance(value, dict), f"Expected {subject} to be a JSON object."
    return typ.cast("dict[str, object]", value)


def _objects(value: object, *, subject: str) -> list[dict[str, object]]:
    """Return a JSON object array, naming the unexpected subject on failure."""
    assert isinstance(value, list), f"Expected {subject} to be a JSON array."
    return [_mapping(item, subject=f"{subject} item") for item in value]


def _text_sequence(value: object, *, subject: str) -> tuple[str, ...]:
    """Return a JSON string array, naming the unexpected subject on failure."""
    assert isinstance(value, list), f"Expected {subject} to be a JSON array."
    assert all(isinstance(item, str) for item in value), (
        f"Expected {subject} to contain only JSON strings."
    )
    return tuple(typ.cast("list[str]", value))


def _makefile_report() -> dict[str, object]:
    """Return Makeutil's complete, successfully parsed Makefile report."""
    completed = subprocess.run(  # noqa: S603 - fixed parser command.
        _MAKEUTIL_COMMAND,
        capture_output=True,
        check=True,
        cwd=REPOSITORY_ROOT,
        text=True,
    )
    report = typ.cast("dict[str, object]", json.loads(completed.stdout))
    parse = _mapping(report.get("parse"), subject="Makeutil parse report")
    assert parse.get("status") == "complete", (
        f"Expected Makeutil to complete the Makefile parse, got {parse!r}."
    )
    return report


def _sole_variable(name: str) -> dict[str, object]:
    """Return Makeutil's sole variable fact for ``name``."""
    variables = _objects(_makefile_report().get("variables"), subject="variables")
    matches = [variable for variable in variables if variable.get("name") == name]
    assert len(matches) == 1, (
        f"Expected exactly one Makefile variable named {name!r}, found {len(matches)}."
    )
    return matches[0]


def _sole_recipe_rule(target: str) -> dict[str, object]:
    """Return the only parsed rule for ``target`` that contains recipes."""
    rules = _objects(_makefile_report().get("rules"), subject="rules")
    matches = [
        rule
        for rule in rules
        if target in _text_sequence(rule.get("targets"), subject="rule targets")
        and _objects(rule.get("recipes"), subject="rule recipes")
    ]
    assert len(matches) == 1, (
        f"Expected exactly one recipe-bearing Makefile rule named {target!r}, "
        f"found {len(matches)}."
    )
    return matches[0]


def _variable_tokens(name: str) -> tuple[str, ...]:
    """Return shell-like tokens from Makeutil's raw variable value."""
    value = _sole_variable(name).get("raw_value")
    assert isinstance(value, str), f"Expected {name!r} to have a string value."
    return tuple(shlex.split(value))


def _recipe_tokens(target: str) -> tuple[tuple[str, ...], ...]:
    """Return shell-like tokens for every recipe in ``target``."""
    recipes = _objects(
        _sole_recipe_rule(target).get("recipes"), subject=f"{target} recipes"
    )
    return tuple(
        tuple(shlex.split(recipe_text.replace("\\\n", "")))
        for recipe in recipes
        if isinstance(recipe_text := recipe.get("text"), str)
    )


def _workflow_job(job_name: str) -> dict[str, object]:
    """Return the named job from the main CI workflow."""
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    workflow_mapping = _mapping(workflow, subject="CI workflow")
    jobs = _mapping(workflow_mapping.get("jobs"), subject="CI workflow jobs")
    return _mapping(jobs.get(job_name), subject=f"CI job {job_name!r}")


def _sole_workflow_step(job_name: str, step_name: str) -> dict[str, object]:
    """Return the sole named CI step from ``job_name``."""
    job = _workflow_job(job_name)
    steps = _objects(job.get("steps"), subject=f"CI job {job_name!r} steps")
    matches = [step for step in steps if step.get("name") == step_name]
    assert len(matches) == 1, (
        f"Expected exactly one {step_name!r} step in CI job {job_name!r}, found "
        f"{len(matches)}."
    )
    return matches[0]


def _make_executable() -> str:
    """Return the available Make executable for subprocess contract tests."""
    executable = shutil.which("make")
    if executable is None:
        message = "Expected make to be available for this test."
        raise RuntimeError(message)
    return executable


def _run_skylos_allow(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the whitelist boundary without invoking Skylos on valid input."""
    environment: dict[str, str] = dict(os.environ)
    environment["NAME"] = "wsl-hostname"
    environment.pop("REASON", None)
    environment.pop("SYMBOL", None)
    command: list[str] = [_make_executable(), "skylos-allow", *arguments]
    return subprocess.run(  # noqa: S603 - fixed Make target and arguments.
        command,
        capture_output=True,
        check=False,
        cwd=REPOSITORY_ROOT,
        env=environment,
        text=True,
    )


def _assert_makeutil_installation(command: object, *, contract: str) -> None:
    """Assert that ``command`` installs the pinned Makeutil parser."""
    assert isinstance(command, str), (
        f"{contract} must provide a Makeutil installation shell command."
    )
    assert (
        tuple(shlex.split(command.replace("\\\n", ""))) == _MAKEUTIL_INSTALL_TOKENS
    ), f"{contract} must pin the complete Makeutil installation command."


def test_lint_recipe_runs_the_production_dead_code_gate() -> None:
    """``make lint`` must scan production code with Skylos's strict gate."""
    assert _variable_tokens("SKYLOS_VERSION") == ("4.33.2",), (
        "Skylos version contract must pin version 4.33.2."
    )
    assert _variable_tokens("SKYLOS_PRODUCTION_TARGETS") == (
        "post_turn_quality_stop_hook",
    ), "Skylos production-target contract must scan only the package."
    assert _variable_tokens("SKYLOS_EXCLUDE_FOLDERS") == ("tests",), (
        "Skylos exclusion contract must omit test code."
    )

    skylos_commands = [
        command for command in _recipe_tokens("lint") if command[:1] == ("$(SKYLOS)",)
    ]
    assert skylos_commands == [
        (
            "$(SKYLOS)",
            "$(SKYLOS_PRODUCTION_TARGETS)",
            "--exclude",
            "$(SKYLOS_EXCLUDE_FOLDERS)",
            "--category",
            "dead_code",
            "--gate",
            "--format",
            "concise",
            "--no-upload",
            "--no-provenance",
            "--no-grep-verify",
        )
    ], "Skylos lint command must strictly scan production dead code only."


def test_whitelist_target_uses_the_skylos_subcommand_contract() -> None:
    """``skylos whitelist`` must run before its arguments and scan options."""
    assert _variable_tokens("SKYLOS_CLI") == (
        "$(UV_ENV)",
        "uv",
        "tool",
        "run",
        "--python",
        "3.14",
        "--from",
        "skylos==$(SKYLOS_VERSION)",
        "skylos",
    ), "Skylos CLI contract must pin Python 3.14 and the tool release."
    assert _variable_tokens("SKYLOS") == (
        "$(SKYLOS_CLI)",
        "--config-file",
        "pyproject.toml",
    ), "Skylos scan command must add only its configuration option."

    whitelist_commands = [
        command
        for command in _recipe_tokens("skylos-allow")
        if command[:1] == ("$(SKYLOS_CLI)",)
    ]
    assert whitelist_commands == [
        (
            "$(SKYLOS_CLI)",
            "whitelist",
            "$${SKYLOS_SYMBOL}",
            "--reason",
            "$${SKYLOS_REASON}",
        )
    ], "Skylos whitelist command must dispatch before its reason option."


def test_skylos_allow_requires_symbol_and_reason() -> None:
    """The whitelist target must reject incomplete input without running Skylos."""
    for arguments, expected_error in (
        ((), "Error: SYMBOL is required for a named whitelist exception"),
        (
            ("SYMBOL=handler",),
            "Error: REASON is required for a named whitelist exception",
        ),
    ):
        completed = _run_skylos_allow(*arguments)
        assert completed.returncode == _MISSING_ARGUMENT_EXIT_CODE, (
            "Skylos whitelist boundary must reject missing required arguments "
            f"for {arguments!r}."
        )
        assert expected_error in completed.stderr, (
            "Skylos whitelist boundary must name the missing required argument "
            f"for {arguments!r}."
        )


def test_skylos_allow_dry_run_preserves_the_whitelist_command_contract() -> None:
    """A valid dry run must reveal the command without mutating the allow list."""
    completed = subprocess.run(  # noqa: S603 - fixed Make target and arguments.
        (
            _make_executable(),
            "--dry-run",
            "skylos-allow",
            "SYMBOL=handler",
            "REASON=Loaded by plugin registry",
        ),
        capture_output=True,
        check=False,
        cwd=REPOSITORY_ROOT,
        text=True,
    )

    assert completed.returncode == 0, (
        "Skylos whitelist dry-run contract must accept complete input."
    )
    assert (
        'skylos whitelist "${SKYLOS_SYMBOL}" --reason "${SKYLOS_REASON}"'
        in completed.stdout
    ), "Skylos whitelist dry-run contract must preserve subcommand argument order."


def test_skylos_configuration_models_runtime_callers_before_allowing_them() -> None:
    """Static-analysis exceptions remain typed, narrow, and explained."""
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as configuration_file:
        configuration = tomllib.load(configuration_file)

    tool = _mapping(configuration.get("tool"), subject="tool configuration")
    skylos = _mapping(tool.get("skylos"), subject="Skylos configuration")
    gate = _mapping(skylos.get("gate"), subject="Skylos gate configuration")
    assert gate.get("strict") is True, (
        "Skylos gate configuration must enable strict mode."
    )

    dead_code = _mapping(
        skylos.get("dead_code", {}), subject="Skylos dead-code configuration"
    )
    entry_points = _objects(
        dead_code.get("entrypoints", []), subject="Skylos typed entry points"
    )
    for entry_point in entry_points:
        assert isinstance(entry_point.get("type"), str), (
            "Skylos runtime callers must use a typed entry-point rule."
        )
        names = _text_sequence(
            entry_point.get("full_name"), subject="Skylos entry-point names"
        )
        assert names, "Skylos typed entry-point rules must name a runtime caller."
        reason = entry_point.get("reason")
        assert isinstance(reason, str), (
            "Skylos typed entry-point rules must provide a textual reason."
        )
        assert reason.strip(), (
            "Skylos typed entry-point rules must provide a non-empty reason."
        )

    whitelist = _mapping(skylos.get("whitelist"), subject="Skylos whitelist")
    documented = _mapping(
        whitelist.get("documented"), subject="documented Skylos whitelist"
    )
    for symbol, reason in documented.items():
        assert isinstance(symbol, str), (
            "Documented Skylos exceptions must identify a textual symbol."
        )
        assert symbol.strip(), "Documented Skylos exceptions must identify a symbol."
        assert isinstance(reason, str), (
            "Documented Skylos exceptions must provide a textual reason."
        )
        assert reason.strip(), (
            "Documented Skylos exceptions must provide a verified reason."
        )


def test_ci_runs_the_lint_target_and_installs_makeutil() -> None:
    """The full-suite CI job must run lint and provision its Makefile parser."""
    lint_step = _sole_workflow_step(
        "lint-test", "Run lint and Skylos dead-code detection"
    )
    assert lint_step.get("run") == "make lint", (
        "CI lint-step contract must invoke the shared make lint target."
    )

    parser_step = _sole_workflow_step("lint-test", "Install Makefile parser")
    environment = _mapping(
        parser_step.get("env"), subject="CI Makeutil installation environment"
    )
    assert environment.get("MAKEUTIL_REVISION") == _MAKEUTIL_REVISION, (
        "CI Makeutil revision contract must stay pinned."
    )
    assert environment.get("MAKEUTIL_TOOLCHAIN") == _MAKEUTIL_TOOLCHAIN, (
        "CI Makeutil toolchain contract must stay pinned."
    )
    _assert_makeutil_installation(
        parser_step.get("run"), contract="CI Makeutil-install contract"
    )
