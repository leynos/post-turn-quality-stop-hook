"""Build-driver discovery and manifest parsing.

Discovers available build targets from Make and Netsuke manifests,
selects the appropriate build driver based on repository contents,
and parses build-tool output.
"""

from __future__ import annotations

import dataclasses
import shutil
import typing as typ

from post_turn_quality_stop_hook.git import _subprocess_env, run

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.state import StopCheckOptions

SUPPORTED_BUILD_DRIVERS = {"auto", "netsuke", "make"}


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


def parse_makefile(path: Path) -> set[str]:
    """Parse declared targets directly from Makefile rules."""
    targets: set[str] = set()
    for line in _logical_makefile_lines(path.read_text(encoding="utf-8")):
        targets.update(_make_rule_target_names(line))
    return targets


def _logical_makefile_lines(contents: str) -> list[str]:
    """Join Make backslash continuations before reading rule target lists."""
    logical_lines: list[str] = []
    continued_line = ""
    for physical_line in contents.splitlines():
        line = (
            continued_line + physical_line.lstrip() if continued_line else physical_line
        )
        trailing_backslashes = len(line) - len(line.rstrip("\\"))
        if trailing_backslashes % 2:
            continued_line = line[:-1] + " "
            continue
        logical_lines.append(line)
        continued_line = ""
    if continued_line:
        logical_lines.append(continued_line)
    return logical_lines


def _make_rule_target_names(line: str) -> set[str]:
    """Return all target words before the first unescaped rule separator."""
    if line.startswith("\t"):
        return set()
    line = line.lstrip()
    if not line or line.startswith("#"):
        return set()

    separator = _make_rule_separator_index(line)
    if separator is None:
        return set()
    return _split_make_target_words(line[:separator])


def _make_rule_separator_index(line: str) -> int | None:
    """Find a rule separator outside escapes and variable references."""
    reference_depth = 0
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif _starts_make_reference(line, index):
            reference_depth += 1
        elif reference_depth:
            reference_depth += character in "({"
            reference_depth -= character in ")}"
        elif character in "#=":
            return None
        elif character == ":":
            return _make_colon_separator_index(line, index)
    return None


def _make_colon_separator_index(line: str, index: int) -> int | None:
    """Ignore assignment operators that contain a colon."""
    if line.startswith(":=", index) or line.startswith("::=", index):
        return None
    return index


def _starts_make_reference(line: str, index: int) -> bool:
    """Whether a dollar sign begins a parenthesised or braced reference."""
    return line[index] == "$" and index + 1 < len(line) and line[index + 1] in "({"


def _split_make_target_words(target_list: str) -> set[str]:
    """Split a target list, preserving escaped whitespace and delimiters."""
    targets: set[str] = set()
    current_target: list[str] = []
    escaped = False
    for character in target_list:
        if escaped:
            current_target.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character.isspace():
            if current_target:
                targets.add("".join(current_target))
                current_target.clear()
        else:
            current_target.append(character)
    if escaped:
        current_target.append("\\")
    if current_target:
        targets.add("".join(current_target))
    return targets


def parse_netsuke_targets(manifest_stdout: str) -> set[str]:
    """Parse generated Ninja build edges from ``netsuke generate``.

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


def get_make_targets(repo: Path) -> tuple[set[str] | None, str | None]:
    """Collect available Make targets from a repository.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    tuple[set[str] | None, str | None]
        Target set and error message, if any.

    """
    makefile = repo / "Makefile"
    if not makefile.exists():
        return set(), None
    return parse_makefile(makefile), None


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
        p = run([executable, "generate"], repo)
    except FileNotFoundError:
        return None, f"{executable} not found on PATH"

    if p.returncode != 0:
        combined = f"{p.stderr.strip()}\n{p.stdout.strip()}".strip()
        return None, combined or f"{executable} generate failed"

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
    return get_make_targets(repo)


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
        ``(driver, None)`` when a driver is selected, ``(None, error)`` when
        selection fails, or ``(None, None)`` when automatic selection finds
        neither supported manifest and quality targets should be skipped.

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
        ``(driver, None)`` when automatic discovery selects a driver,
        ``(None, error)`` when manifests are present but no usable driver
        remains, or ``(None, None)`` when neither supported manifest exists and
        quality targets should be skipped.

    """
    selected: BuildDriver | None = None
    error: str | None = None

    if availability.has_netsukefile and availability.has_netsuke:
        selected = availability.netsuke
    elif availability.has_makefile and availability.has_make:
        selected = availability.make
    elif not availability.has_netsukefile and not availability.has_makefile:
        selected = None
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
