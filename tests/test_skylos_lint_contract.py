"""Contract tests for the blocking Skylos dead-code lint gate."""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 - test executes make without a shell
import tomllib
import typing as typ
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


def _pyproject() -> dict[str, object]:
    """Load the repository's Python project configuration."""
    return tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )


def test_skylos_is_a_pinned_external_lint_tool() -> None:
    """Keep the deterministic dead-code scanner out of the project environment."""
    config = _pyproject()
    dependency_groups = typ.cast("dict[str, list[str]]", config["dependency-groups"])

    assert not any(
        dependency.startswith("skylos") for dependency in dependency_groups["dev"]
    ), "Expected Skylos to be separately provisioned from the dev group."

    makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "SKYLOS_VERSION = 4.33.2" in makefile
    assert "--from 'skylos==$(SKYLOS_VERSION)' skylos" in makefile


def test_make_lint_runs_blocking_production_dead_code_scan() -> None:
    """Keep the Skylos invocation deterministic and limited to production code."""
    make_executable = shutil.which("make")
    assert make_executable is not None, "Expected make to be available for the test."

    result = subprocess.run(  # noqa: S603 - test executes make without a shell
        [make_executable, "--no-print-directory", "--dry-run", "lint"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, "Expected make lint dry run to succeed."
    dry_run = " ".join(line.rstrip("\\").strip() for line in result.stdout.splitlines())
    skylos_commands = [
        command
        for command in dry_run.split("  ")
        if "skylos --config-file pyproject.toml" in command
    ]
    assert len(skylos_commands) == 1, "Expected one blocking Skylos command."
    skylos_command = skylos_commands[0]
    assert "post_turn_quality_stop_hook" in skylos_command
    assert " tests" not in skylos_command
    assert (
        "--category dead_code --gate --format concise --no-upload "
        "--no-provenance --no-grep-verify" in skylos_command
    )


def test_skylos_configuration_requires_a_reason_for_each_exception() -> None:
    """Make every future static-analysis exception reviewable."""
    tool_config = typ.cast("dict[str, object]", _pyproject()["tool"])
    skylos = typ.cast("dict[str, object]", tool_config["skylos"])
    gate = typ.cast("dict[str, object]", skylos["gate"])
    whitelist = typ.cast("dict[str, object]", skylos["whitelist"])
    documented = typ.cast("dict[str, str]", whitelist["documented"])

    assert gate["strict"] is True
    assert all(reason.strip() for reason in documented.values())


def test_skylos_allow_requires_a_name_and_reason() -> None:
    """Prevent undocumented allow-list exceptions from entering the configuration."""
    makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")

    required_fragments = (
        "skylos-allow: ## Document one named Skylos false positive",
        "skylos-allow: export SKYLOS_NAME = $(value NAME)",
        "skylos-allow: export SKYLOS_REASON = $(value REASON)",
        'test -n "$${SKYLOS_NAME}"',
        'test -n "$${SKYLOS_REASON}"',
        "NAME is required for a named Skylos exception",
        "REASON is required for a named Skylos exception",
        'whitelist "$${SKYLOS_NAME}" --reason "$${SKYLOS_REASON}"',
    )

    assert all(fragment in makefile for fragment in required_fragments)


def test_ci_labels_the_skylos_dead_code_gate() -> None:
    """Keep the CI invocation visibly aligned with the local lint target."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "name: Run lint and Skylos dead-code detection" in workflow
    assert "run: make lint" in workflow
