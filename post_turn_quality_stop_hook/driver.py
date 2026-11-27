"""Build-driver discovery and manifest parsing.

Discovers available build targets from Make and Netsuke manifests,
selects the appropriate build driver based on repository contents,
and parses build-tool output.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import typing as typ

from post_turn_quality_stop_hook.errors import HookError
from post_turn_quality_stop_hook.process import (
    LAUNCH_FAILURE_STATUS,
    LaunchedProcess,
    _subprocess_env,
    run,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from post_turn_quality_stop_hook.state import StopCheckOptions

SUPPORTED_BUILD_DRIVERS = {"auto", "netsuke", "make"}

#: Fixed reasons recorded for a driver selected by configuration. The text is
#: bounded and path-free, so a record never carries an executable path or an
#: environment value.
EXPLICIT_DRIVER_REASON = "selected by configuration"


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
    selection_reason
        Fixed explanation of why this driver was selected, filled by
        :func:`select_build_driver`. It never carries a path or an environment
        value, so it is safe to record.

    """

    name: str
    executable: str
    manifest: str
    selection_reason: str = ""


def _automatic_driver_reason(driver: BuildDriver) -> str:
    """Return the fixed explanation for an automatically selected driver."""
    return f"selected automatically from the {driver.manifest}"


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
    # Codec-name re-casing is equivalent and encoding=None only differs under
    # a non-UTF-8 locale, so this read is a mutation boundary.
    contents = path.read_text(encoding="utf-8")  # pragma: no mutate
    for line in _logical_makefile_lines(contents):
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


@dataclasses.dataclass(slots=True, frozen=True)
class TargetEnumeration:
    """Declared build targets, with the status of the command that read them.

    Attributes
    ----------
    available
        Declared target names, or the failure that prevented reading them.
    exit_status
        Exit status the enumerating command reported. A Makefile is parsed
        without running a command, so that read reports ``0`` and emits no
        command record.
    launched
        Whether the enumerating command actually started. ``False`` marks a
        read whose executable could not be launched at all, so there is no
        child status to report.

    """

    available: frozenset[str] | HookError
    exit_status: int = 0
    launched: bool = True

    @property
    def recorded_exit_status(self) -> int:
        """Status a command record should carry for this read.

        A command that never started has no child status. Recording the shared
        launch-failure value keeps it distinct from a status the child itself
        returned.
        """
        return self.exit_status if self.launched else LAUNCH_FAILURE_STATUS


def get_make_targets(repo: Path) -> TargetEnumeration:
    """Collect available Make targets from a repository.

    A Makefile is read directly, so no command runs and no child status
    exists; the enumeration reports ``0`` and records no command.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    TargetEnumeration
        Declared target names, or a read failure.

    """
    makefile = repo / "Makefile"
    if not makefile.is_file():
        return TargetEnumeration(available=frozenset())
    try:
        return TargetEnumeration(available=frozenset(parse_makefile(makefile)))
    except (OSError, UnicodeError) as exc:
        return TargetEnumeration(
            available=HookError("get_make_targets", f"could not read {makefile}: {exc}")
        )


def get_netsuke_targets(repo: Path, executable: str = "netsuke") -> TargetEnumeration:
    """Collect available Netsuke targets from a repository.

    ``netsuke generate`` is a blocking command, so its own exit status is
    carried with the answer. A launch failure has no child status and reports
    the shared launch-failure value instead.

    Parameters
    ----------
    repo
        Repository root path.
    executable
        Netsuke executable to run.

    Returns
    -------
    TargetEnumeration
        Generated target names, the generation failure, and the status of the
        command that read them.

    """
    try:
        p = run([executable, "generate"], repo)
    except FileNotFoundError:
        return TargetEnumeration(
            available=HookError(
                "get_netsuke_targets", f"{executable} not found on PATH"
            ),
            launched=False,
        )
    except OSError as exc:
        # Every other launch error must become the launch-failure result
        # rather than escape: ``execve`` reports a missing shebang or execute
        # bit as ``ENOEXEC``, and a refused working directory as
        # ``EACCES``. No child ran in any of those cases, so the answer
        # carries the launch-failure status rather than a child status. The
        # operating system's own ``filename`` names the element that failed,
        # which is either the executable or the working directory.
        failed = exc.filename
        if failed is not None and failed == str(repo):
            detail = f"{repo} could not be used as the working directory: {exc}"
        elif failed is not None:
            detail = f"{failed} cannot be executed: {exc}"
        else:
            detail = f"{executable} could not be launched: {exc}"
        return TargetEnumeration(
            available=HookError("get_netsuke_targets", detail),
            launched=False,
        )

    launched = not (isinstance(p, LaunchedProcess) and not p.launched)
    if p.returncode != 0:
        combined = f"{p.stderr.strip()}\n{p.stdout.strip()}".strip()
        return TargetEnumeration(
            available=HookError(
                "get_netsuke_targets", combined or f"{executable} generate failed"
            ),
            exit_status=p.returncode,
            launched=launched,
        )

    return TargetEnumeration(
        available=frozenset(parse_netsuke_targets(p.stdout)), exit_status=0
    )


def get_build_targets(repo: Path, driver: BuildDriver) -> TargetEnumeration:
    """Collect available build targets for a selected driver.

    Parameters
    ----------
    repo
        Repository root path.
    driver
        Build driver to use for target enumeration.

    Returns
    -------
    TargetEnumeration
        Target set or enumeration failure, with the status of the command that
        read it.

    """
    if driver.name == "netsuke":
        return get_netsuke_targets(repo, driver.executable)
    return get_make_targets(repo)


def _is_executable_available(executable: str) -> bool:
    """Return whether an executable can be invoked.

    Driver selection runs before the quality stage and has no composition
    root of its own, so this call site names the environment it reads instead
    of relying on a hidden default inside the environment builder. The
    enriched ``PATH`` a child command would receive decides availability, so
    the answer matches the command the selected driver will run.
    """
    path = _subprocess_env(environ=os.environ)["PATH"]
    return shutil.which(executable, path=path) is not None


def _driver_error(driver: BuildDriver, *, reason: str) -> HookError:
    """Format a build-driver selection error."""
    return HookError("select_build_driver", f"Cannot use {driver.name}: {reason}")


def select_build_driver(
    repo: Path, options: StopCheckOptions
) -> BuildDriver | HookError | None:
    """Select the build driver for repository quality gates.

    Parameters
    ----------
    repo
        Repository root path.
    options
        Stop-hook runtime options.

    Returns
    -------
    BuildDriver | HookError | None
        The selected driver, a selection failure, or ``None`` when automatic
        selection finds neither supported manifest and quality targets should
        be skipped.

    """
    requested_driver = options.build_driver.strip().lower()
    if requested_driver not in SUPPORTED_BUILD_DRIVERS:
        supported = ", ".join(sorted(SUPPORTED_BUILD_DRIVERS))
        return HookError(
            "select_build_driver",
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
) -> BuildDriver | HookError | None:
    """Select a build driver using automatic discovery.

    Parameters
    ----------
    availability
        Driver availability information for the repository.

    Returns
    -------
    BuildDriver | HookError | None
        The selected driver, a selection failure, or ``None`` when neither
        supported manifest exists and quality targets should be skipped.

    """
    selected: BuildDriver | None = None
    error: HookError | None = None

    if availability.has_netsukefile and availability.has_netsuke:
        selected = dataclasses.replace(
            availability.netsuke,
            selection_reason=_automatic_driver_reason(availability.netsuke),
        )
    elif availability.has_makefile and availability.has_make:
        selected = dataclasses.replace(
            availability.make,
            selection_reason=_automatic_driver_reason(availability.make),
        )
    elif not availability.has_netsukefile and not availability.has_makefile:
        selected = None
    elif availability.has_unusable_netsukefile and not availability.has_makefile:
        error = _driver_error(
            availability.netsuke,
            reason=f"{availability.netsuke.executable} not found",
        )
    else:
        error = HookError(
            "select_build_driver",
            "No supported build driver available. Add a Netsukefile with netsuke "
            "on PATH, or add a Makefile with make on PATH.",
        )

    return selected if error is None else error


def _select_required_driver(repo: Path, driver: BuildDriver) -> BuildDriver | HookError:
    """Select an explicitly requested driver or explain why it cannot run.

    Parameters
    ----------
    repo
        Repository root path.
    driver
        Requested build driver.

    Returns
    -------
    BuildDriver | HookError
        Selected driver, or a selection failure.

    """
    if not (repo / driver.manifest).is_file():
        return _driver_error(driver, reason=f"{driver.manifest} is missing")
    if not _is_executable_available(driver.executable):
        return _driver_error(driver, reason=f"{driver.executable} not found")
    return dataclasses.replace(driver, selection_reason=EXPLICIT_DRIVER_REASON)
