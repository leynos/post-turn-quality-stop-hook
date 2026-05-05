"""Build-driver discovery and manifest parsing.

Discovers available build targets from Make and Netsuke manifests,
selects the appropriate build driver based on repository contents,
and parses build-tool output.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import typing as typ

from post_turn_quality_stop_hook.git import _subprocess_env, run

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.state import StopCheckOptions

MAKE_FAILURE_EXIT = 2

SUPPORTED_BUILD_DRIVERS = {"auto", "netsuke", "make"}

MAKE_TARGET_PROBE = "__post_turn_quality_stop_hook_target_probe__"


@dataclasses.dataclass(slots=True, frozen=True)
class BuildDriver:
    """Quality-gate build driver.

    Attributes
    ----------
    name
        Human-readable driver name.
    executable
        Executable path or command name.
    manifest
        Repository manifest file that identifies the driver.

    """

    name: str
    executable: str
    manifest: str


@dataclasses.dataclass(slots=True, frozen=True)
class DriverAvailability:
    """Available build-driver manifests and executables."""

    netsuke: BuildDriver
    make: BuildDriver
    has_netsukefile: bool
    has_makefile: bool
    has_netsuke: bool
    has_make: bool
    has_unusable_netsukefile: bool


def parse_make_targets(make_stdout: str) -> set[str]:
    """Parse make database output for target names.

    Parameters
    ----------
    make_stdout
        Stdout from make -p.

    Returns
    -------
    set[str]
        Parsed Make target names.

    """
    targets: set[str] = set()
    rule_re = re.compile(r"^([^\s:#=]+(?:\s+[^\s:#=]+)*)\s*(?:::|\:(?!\=))\s*.*$")
    for line in make_stdout.splitlines():
        if not line:
            continue
        if line.startswith(("#", "\t", " ")):
            continue
        m = rule_re.match(line)
        if not m:
            continue
        lhs = m.group(1)
        for t in lhs.split():
            if "%" in t:
                continue
            if t != MAKE_TARGET_PROBE:
                targets.add(t)
    return targets


def parse_netsuke_targets(manifest_stdout: str) -> set[str]:
    """Parse generated Ninja build edges from ``netsuke manifest -``.

    Parameters
    ----------
    manifest_stdout
        Generated Ninja manifest printed by Netsuke.

    Returns
    -------
    set[str]
        Parsed explicit build target names.

    """
    targets: set[str] = set()
    for line in manifest_stdout.splitlines():
        if not line.startswith("build "):
            continue
        outputs, _separator, _rule = line.removeprefix("build ").partition(":")
        for output in outputs.split():
            if output.startswith(("$", "|")):
                continue
            targets.add(output)
    return targets


def is_missing_makefile(output: str) -> bool:
    """Check output for a missing Makefile condition.

    Parameters
    ----------
    output
        Combined output from make.

    Returns
    -------
    bool
        True if the output indicates no Makefile was found.

    """
    lowered = output.lower()
    return "no makefile found" in lowered


def get_make_targets(
    repo: Path, executable: str = "make"
) -> tuple[set[str] | None, str | None]:
    """Collect available Make targets from a repository.

    Parameters
    ----------
    repo
        Repository root path.
    executable
        Make executable to run.

    Returns
    -------
    tuple[set[str] | None, str | None]
        Target set and error message, if any.

    """
    try:
        p = run(
            [
                executable,
                "-p",
                "--no-print-directory",
                f"--eval={MAKE_TARGET_PROBE}:",
                MAKE_TARGET_PROBE,
            ],
            repo,
        )
    except FileNotFoundError:
        return None, f"{executable} not found on PATH"

    if p.returncode == MAKE_FAILURE_EXIT:
        combined = f"{p.stderr.strip()}\n{p.stdout.strip()}".strip()
        if is_missing_makefile(combined):
            return set(), None
        return None, combined or "make -p failed"

    return parse_make_targets(p.stdout), None


def get_netsuke_targets(
    repo: Path, executable: str = "netsuke"
) -> tuple[set[str] | None, str | None]:
    """Collect available Netsuke targets from a repository.

    Parameters
    ----------
    repo
        Repository root path.
    executable
        Netsuke executable to run.

    Returns
    -------
    tuple[set[str] | None, str | None]
        Target set and error message, if any.

    """
    try:
        p = run([executable, "manifest", "-"], repo)
    except FileNotFoundError:
        return None, f"{executable} not found on PATH"

    if p.returncode != 0:
        combined = f"{p.stderr.strip()}\n{p.stdout.strip()}".strip()
        return None, combined or f"{executable} manifest - failed"

    return parse_netsuke_targets(p.stdout), None


def get_build_targets(
    repo: Path, driver: BuildDriver
) -> tuple[set[str] | None, str | None]:
    """Collect available build targets for a selected driver.

    Parameters
    ----------
    repo
        Repository root path.
    driver
        Build driver to use for target enumeration.

    Returns
    -------
    tuple[set[str] | None, str | None]
        Target set and error message, if any.

    """
    if driver.name == "netsuke":
        return get_netsuke_targets(repo, driver.executable)
    return get_make_targets(repo, driver.executable)


def _is_executable_available(executable: str) -> bool:
    """Return whether an executable can be invoked."""
    return shutil.which(executable, path=_subprocess_env()["PATH"]) is not None


def _driver_error(driver: BuildDriver, *, reason: str) -> str:
    """Format a build-driver selection error."""
    return f"Cannot use {driver.name}: {reason}"


def select_build_driver(
    repo: Path, options: StopCheckOptions
) -> tuple[BuildDriver | None, str | None]:
    """Select the build driver for repository quality gates.

    Parameters
    ----------
    repo
        Repository root path.
    options
        Stop-hook runtime options.

    Returns
    -------
    tuple[BuildDriver | None, str | None]
        Selected build driver and error message, if no driver can be selected.

    """
    requested_driver = options.build_driver.strip().lower()
    if requested_driver not in SUPPORTED_BUILD_DRIVERS:
        supported = ", ".join(sorted(SUPPORTED_BUILD_DRIVERS))
        return (
            None,
            f"Unsupported build driver '{options.build_driver}'. Use {supported}.",
        )

    netsuke = BuildDriver("netsuke", options.netsuke_bin, "Netsukefile")
    make = BuildDriver("make", options.make_bin, "Makefile")
    availability = DriverAvailability(
        netsuke=netsuke,
        make=make,
        has_netsukefile=(repo / netsuke.manifest).is_file(),
        has_makefile=(repo / make.manifest).is_file(),
        has_netsuke=_is_executable_available(netsuke.executable),
        has_make=_is_executable_available(make.executable),
        has_unusable_netsukefile=(repo / netsuke.manifest).is_file()
        and not _is_executable_available(netsuke.executable),
    )
    if requested_driver == "netsuke":
        result = _select_required_driver(repo, netsuke)
    elif requested_driver == "make":
        result = _select_required_driver(repo, make)
    else:
        result = _select_auto_driver(availability)

    return result


def _select_auto_driver(
    availability: DriverAvailability,
) -> tuple[BuildDriver | None, str | None]:
    """Select a build driver using automatic discovery.

    Parameters
    ----------
    availability
        Driver availability information for the repository.

    Returns
    -------
    tuple[BuildDriver | None, str | None]
        Selected driver and error message, if any.

    """
    selected: BuildDriver | None = None
    error: str | None = None

    if availability.has_netsukefile and availability.has_netsuke:
        selected = availability.netsuke
    elif availability.has_makefile and availability.has_make:
        selected = availability.make
    elif availability.has_unusable_netsukefile and not availability.has_makefile:
        error = _driver_error(
            availability.netsuke,
            reason=f"{availability.netsuke.executable} not found",
        )
    else:
        error = (
            "No supported build driver available. Add a Netsukefile with netsuke "
            "on PATH, or add a Makefile with make on PATH."
        )

    return selected, error


def _select_required_driver(
    repo: Path, driver: BuildDriver
) -> tuple[BuildDriver | None, str | None]:
    """Select an explicitly requested driver or explain why it cannot run.

    Parameters
    ----------
    repo
        Repository root path.
    driver
        Requested build driver.

    Returns
    -------
    tuple[BuildDriver | None, str | None]
        Selected driver and error message, if any.

    """
    if not (repo / driver.manifest).is_file():
        return None, _driver_error(driver, reason=f"{driver.manifest} is missing")
    if not _is_executable_available(driver.executable):
        return None, _driver_error(driver, reason=f"{driver.executable} not found")
    return driver, None
