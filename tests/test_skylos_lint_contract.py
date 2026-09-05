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
import string
import subprocess  # noqa: S404 - contract tests invoke fixed local commands.
import sys
import tomllib
import typing as typ
from pathlib import Path
from tempfile import TemporaryDirectory

import hypothesis as hyp
import hypothesis.strategies as st
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"

_MAKEUTIL_COMMAND: typ.Final = ("makeutil", "parse", "Makefile")
_MAKEUTIL_REVISION: typ.Final = "29fc5a1634ffbaa18a773eed9dff1b2838a45d9c"
_MAKEUTIL_TOOLCHAIN: typ.Final = "nightly-2026-05-28"
_MISSING_ARGUMENT_EXIT_CODE: typ.Final = 2
_SKYLOS_VERSION_TOKENS: typ.Final = ("4.33.2",)
_SKYLOS_PRODUCTION_TARGET_TOKENS: typ.Final = ("post_turn_quality_stop_hook",)
_SKYLOS_EXCLUDE_FOLDERS_TOKENS: typ.Final = ("tests",)
_SKYLOS_CLI_TOKENS: typ.Final = (
    "$(UV_ENV)",
    "uv",
    "tool",
    "run",
    "--python",
    "3.14",
    "--from",
    "skylos==$(SKYLOS_VERSION)",
    "skylos",
)
_SKYLOS_SCAN_TOKENS: typ.Final = (
    "$(SKYLOS_CLI)",
    "--config-file",
    "pyproject.toml",
)
_SKYLOS_LINT_COMMANDS: typ.Final = (
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
    ),
)
_SKYLOS_WHITELIST_LOCK_TOKENS: typ.Final = (".skylos-whitelist.lock",)
_SKYLOS_WHITELIST_COMMANDS: typ.Final = (
    (
        "flock",
        "$(SKYLOS_WHITELIST_LOCK)",
        "env",
        "$(SKYLOS_CLI)",
        "whitelist",
        "$${SKYLOS_SYMBOL}",
        "--reason",
        "$${SKYLOS_REASON}",
    ),
)
_EXPECTED_SKYLOS_ENTRY_POINT_NAMES: typ.Final[frozenset[str]] = frozenset()
_EXPECTED_DOCUMENTED_WHITELIST_NAMES: typ.Final[frozenset[str]] = frozenset()
_SHELL_ARGUMENT_TEXT: typ.Final = st.builds(
    lambda prefix, content, suffix: prefix + content + suffix,
    st.text(alphabet=" \t", max_size=4),
    st.text(
        alphabet=string.ascii_letters + string.digits + "_$;|&'\"()[]{}*?!\\`",
        min_size=1,
        max_size=40,
    ),
    st.text(alphabet=" \t", max_size=4),
)
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


def _recipe_prerequisites(target: str) -> tuple[str, ...]:
    """Return the prerequisites attached to ``target``'s parsed recipe rule."""
    return _text_sequence(
        _sole_recipe_rule(target).get("prerequisites"),
        subject=f"{target} prerequisites",
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


def _run_skylos_allow(
    *,
    environment_overrides: dict[str, str] | None = None,
    make_variables: tuple[str, ...] = (),
    working_directory: Path = REPOSITORY_ROOT,
) -> subprocess.CompletedProcess[str]:
    """Run the whitelist boundary with a WSL-like environment."""
    environment: dict[str, str] = dict(os.environ)
    environment["NAME"] = "wsl-hostname"
    environment.pop("REASON", None)
    environment.pop("SYMBOL", None)
    if environment_overrides is not None:
        environment.update(environment_overrides)
    command: list[str] = [
        _make_executable(),
        "--no-print-directory",
        "-f",
        str(REPOSITORY_ROOT / "Makefile"),
        *make_variables,
        "skylos-allow",
    ]
    return subprocess.run(  # noqa: S603 - fixed Make target and arguments.
        command,
        capture_output=True,
        check=False,
        cwd=working_directory,
        env=environment,
        text=True,
    )


def _skylos_allow_command(make_variables: tuple[str, ...]) -> tuple[str, ...]:
    """Build the fixed Make invocation for the whitelist helper."""
    return (
        _make_executable(),
        "--no-print-directory",
        "-f",
        str(REPOSITORY_ROOT / "Makefile"),
        *make_variables,
        "skylos-allow",
    )


def _whitelist_lock_variable(directory: Path) -> str:
    """Return the Make override for an isolated Skylos whitelist lock."""
    return f"SKYLOS_WHITELIST_LOCK={directory / '.skylos-whitelist.lock'}"


def _write_whitelist_writer(directory: Path) -> str:
    """Create a deterministic fake CLI that records a documented entry."""
    writer = directory / "write_whitelist_entry.py"
    writer.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n\n"
        "symbol = sys.argv[2]\n"
        "reason = sys.argv[4]\n"
        "configuration = Path('pyproject.toml')\n"
        "contents = configuration.read_text(encoding='utf-8')\n"
        "time.sleep(0.2)\n"
        "configuration.write_text(\n"
        "    contents + f'{symbol} = {reason!r}\\n', encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(writer))}"


def _start_whitelist_update(
    directory: Path,
    make_variables: tuple[str, ...],
    *,
    symbol: str,
    reason: str,
) -> subprocess.Popen[str]:
    """Start a whitelist update whose fake CLI deliberately overlaps another."""
    return subprocess.Popen(  # noqa: S603 - fixed Makefile and test arguments.
        _skylos_allow_command(make_variables),
        cwd=directory,
        env={**os.environ, "SYMBOL": symbol, "REASON": reason},
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )


def _assert_whitelist_updates_succeeded(
    updates: tuple[subprocess.Popen[str], ...],
) -> None:
    """Assert every concurrently started whitelist update completed cleanly."""
    for update in updates:
        stdout, stderr = update.communicate()
        assert update.returncode == 0, (
            "Concurrent Skylos whitelist update must succeed while holding the lock: "
            f"{stdout}{stderr}"
        )


def _documented_whitelist_entries(configuration_path: Path) -> dict[str, str]:
    """Load the documented exceptions produced by a fake Skylos writer."""
    with configuration_path.open("rb") as configuration_file:
        configuration = tomllib.load(configuration_file)
    return configuration["tool"]["skylos"]["whitelist"]["documented"]


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
    assert _variable_tokens("SKYLOS_VERSION") == _SKYLOS_VERSION_TOKENS, (
        "Skylos version contract must pin the reviewed Skylos release."
    )
    assert (
        _variable_tokens("SKYLOS_PRODUCTION_TARGETS")
        == _SKYLOS_PRODUCTION_TARGET_TOKENS
    ), "Skylos production-target contract must scan only the package."
    assert (
        _variable_tokens("SKYLOS_EXCLUDE_FOLDERS") == _SKYLOS_EXCLUDE_FOLDERS_TOKENS
    ), "Skylos exclusion contract must omit test code."

    skylos_commands = [
        command for command in _recipe_tokens("lint") if command[:1] == ("$(SKYLOS)",)
    ]
    assert tuple(skylos_commands) == _SKYLOS_LINT_COMMANDS, (
        "Skylos lint command must strictly scan production dead code only."
    )


def test_full_local_suite_requires_the_makefile_parser() -> None:
    """The full local suite must provision Makeutil before parsing the Makefile."""
    assert "makeutil" in _recipe_prerequisites("test"), (
        "Local full-suite contract must require the pinned Makeutil parser."
    )


def test_whitelist_target_uses_the_skylos_subcommand_contract() -> None:
    """``skylos whitelist`` must run before its arguments and scan options."""
    assert _variable_tokens("SKYLOS_CLI") == _SKYLOS_CLI_TOKENS, (
        "Skylos CLI contract must pin Python 3.14 and the tool release."
    )
    assert _variable_tokens("SKYLOS") == _SKYLOS_SCAN_TOKENS, (
        "Skylos scan command must add only its configuration option."
    )
    assert _variable_tokens("SKYLOS_WHITELIST_LOCK") == _SKYLOS_WHITELIST_LOCK_TOKENS, (
        "Skylos whitelist contract must use the repository-local lock path."
    )
    assert (
        _SKYLOS_WHITELIST_LOCK_TOKENS[0]
        in (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    ), "Skylos whitelist lock contract must ignore the repository-local lock file."

    whitelist_commands = [
        command
        for command in _recipe_tokens("skylos-allow")
        if command[:4] == ("flock", "$(SKYLOS_WHITELIST_LOCK)", "env", "$(SKYLOS_CLI)")
    ]
    assert tuple(whitelist_commands) == _SKYLOS_WHITELIST_COMMANDS, (
        "Skylos whitelist command must lock and dispatch before its reason option."
    )


def test_skylos_allow_requires_symbol_and_reason() -> None:
    """The whitelist target must reject incomplete input without running Skylos."""
    pyproject_path = REPOSITORY_ROOT / "pyproject.toml"
    original_configuration = pyproject_path.read_bytes()
    for environment_overrides, expected_error in (
        ({}, "Error: SYMBOL is required for a named whitelist exception"),
        (
            {"SYMBOL": "handler"},
            "Error: REASON is required for a named whitelist exception",
        ),
    ):
        completed = _run_skylos_allow(
            environment_overrides=environment_overrides,
        )
        assert completed.returncode == _MISSING_ARGUMENT_EXIT_CODE, (
            "Skylos whitelist boundary must reject missing required arguments "
            f"for {environment_overrides!r}, even when WSL supplies NAME."
        )
        assert expected_error in completed.stderr, (
            "Skylos whitelist boundary must name the missing required argument "
            f"for {environment_overrides!r}."
        )
    assert pyproject_path.read_bytes() == original_configuration, (
        "Missing Skylos whitelist inputs must not mutate pyproject.toml."
    )


@hyp.settings(max_examples=25, deadline=None)
@hyp.given(value=st.text(alphabet=" \t", min_size=1, max_size=8))
def test_skylos_allow_rejects_whitespace_only_values(value: str) -> None:
    """The whitelist target must reject whitespace-only symbol and reason values."""
    pyproject_path = REPOSITORY_ROOT / "pyproject.toml"
    original_configuration = pyproject_path.read_bytes()
    for environment_overrides, missing_name in (
        ({"SYMBOL": value, "REASON": "verified caller"}, "SYMBOL"),
        ({"SYMBOL": "handler", "REASON": value}, "REASON"),
    ):
        completed = _run_skylos_allow(
            environment_overrides=environment_overrides,
        )
        assert completed.returncode == _MISSING_ARGUMENT_EXIT_CODE, (
            f"Skylos whitelist boundary must reject whitespace-only {missing_name}."
        )
        assert (
            f"Error: {missing_name} is required for a named whitelist exception"
            in completed.stderr
        ), f"Skylos whitelist boundary must name whitespace-only {missing_name}."
    assert pyproject_path.read_bytes() == original_configuration, (
        "Whitespace-only Skylos whitelist inputs must not mutate pyproject.toml."
    )


@hyp.settings(max_examples=25, deadline=None)
@hyp.example(symbol=" $(handler);* ", reason=' Loaded "$plugin" | registry ')
@hyp.given(symbol=_SHELL_ARGUMENT_TEXT, reason=_SHELL_ARGUMENT_TEXT)
def test_skylos_allow_forwards_generated_argument_boundaries(
    symbol: str, reason: str
) -> None:
    """Every generated non-empty value must reach Skylos as exactly one argument."""
    pyproject_path = REPOSITORY_ROOT / "pyproject.toml"
    original_configuration = pyproject_path.read_bytes()
    with TemporaryDirectory() as temporary_directory:
        recorded_arguments = Path(temporary_directory, "arguments.json")
        recorder = Path(temporary_directory, "skylos-recorder")
        recorder.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n\n"
            'Path(os.environ["SKYLOS_ARGUMENTS_PATH"]).write_text(\n'
            "    json.dumps(sys.argv[1:]), encoding='utf-8'\n"
            ")\n",
            encoding="utf-8",
        )
        recorder.chmod(0o700)
        completed = _run_skylos_allow(
            environment_overrides={
                "SKYLOS_ARGUMENTS_PATH": str(recorded_arguments),
                "SYMBOL": symbol,
                "REASON": reason,
            },
            make_variables=(
                f"SKYLOS_CLI={recorder}",
                _whitelist_lock_variable(Path(temporary_directory)),
            ),
            working_directory=Path(temporary_directory),
        )

        assert completed.returncode == 0, (
            "Skylos whitelist forwarding contract must accept complete generated "
            f"input: {completed.stderr}"
        )
        assert recorded_arguments.is_file(), (
            "Skylos whitelist forwarding contract must invoke the temporary recorder."
        )
        assert json.loads(recorded_arguments.read_text(encoding="utf-8")) == [
            "whitelist",
            symbol,
            "--reason",
            reason,
        ], "Skylos must receive each generated value as exactly one argument."

    assert pyproject_path.read_bytes() == original_configuration, (
        "Valid Skylos whitelist forwarding tests must not mutate pyproject.toml."
    )


def test_skylos_allow_lock_preserves_concurrent_documented_entries() -> None:
    """The repository-local lock must serialize concurrent whitelist writes."""
    with TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        configuration_path = directory / "pyproject.toml"
        configuration_path.write_text(
            "[tool.skylos.whitelist.documented]\n", encoding="utf-8"
        )
        writer = _write_whitelist_writer(directory)
        common_variables = (
            f"SKYLOS_CLI={writer}",
            _whitelist_lock_variable(directory),
        )
        updates = (
            _start_whitelist_update(
                directory,
                common_variables,
                symbol="first",
                reason="first reason",
            ),
            _start_whitelist_update(
                directory,
                common_variables,
                symbol="second",
                reason="second reason",
            ),
        )
        _assert_whitelist_updates_succeeded(updates)
        documented = _documented_whitelist_entries(configuration_path)

    assert documented == {"first": "first reason", "second": "second reason"}, (
        "Skylos whitelist lock must preserve every concurrent documented entry."
    )


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
    entry_point_names: set[str] = set()
    for entry_point in entry_points:
        assert isinstance(entry_point.get("type"), str), (
            "Skylos runtime callers must use a typed entry-point rule."
        )
        names = _text_sequence(
            entry_point.get("full_name"), subject="Skylos entry-point names"
        )
        assert names, "Skylos typed entry-point rules must name a runtime caller."
        entry_point_names.update(names)
        reason = entry_point.get("reason")
        assert isinstance(reason, str), (
            "Skylos typed entry-point rules must provide a textual reason."
        )
        assert reason.strip(), (
            "Skylos typed entry-point rules must provide a non-empty reason."
        )
    assert frozenset(entry_point_names) == _EXPECTED_SKYLOS_ENTRY_POINT_NAMES, (
        "Skylos entry-point contract must pin every approved runtime caller."
    )

    whitelist = _mapping(skylos.get("whitelist"), subject="Skylos whitelist")
    documented = _mapping(
        whitelist.get("documented"), subject="documented Skylos whitelist"
    )
    assert frozenset(documented) == _EXPECTED_DOCUMENTED_WHITELIST_NAMES, (
        "Skylos documented-whitelist contract must pin every approved exception."
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
