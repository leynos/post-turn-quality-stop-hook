"""Free-space policy for the stop hook.

A stop hook that runs quality gates has nothing to gain from blocking the stop
when the disk is full: no agent action can free space, so every retry fails the
same way. This module owns the pure decision about whether a volume is too full
to work in, the environment rule for locating the temporary directory, and the
classification of a failed command's output as disk exhaustion. Probing is done
through ``shutil.disk_usage`` but never raises: an unreadable volume is
reported, not guessed at.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import tempfile
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: Conservative default free-space floor, in mebibytes. Quality gates that
#: write caches and build output fail well below this, and the value is small
#: enough that a healthy machine never trips it.
DEFAULT_MIN_FREE_MB = 100

_BYTES_PER_KIB = 1024
_BYTES_PER_MIB = _BYTES_PER_KIB * _BYTES_PER_KIB

#: Placeholders written by the operating system when a write fails for lack of
#: space. GNU libc and Rust use the "no space left"/"os error 28" spellings;
#: ``ENOSPC`` is the errno name that tools such as compilers and package
#: managers print directly. The word boundary keeps a longer identifier such
#: as ``NOENOSPC`` from matching.
_DISK_EXHAUSTION = re.compile(
    r"no space left on device|\benospc\b|\bos error 28\b",
    re.IGNORECASE,
)

_BINARY_UNITS = ("KiB", "MiB", "GiB", "TiB")


@dataclasses.dataclass(slots=True, frozen=True)
class LowSpace:
    """A volume measured below the configured free-space floor.

    Attributes
    ----------
    path
        Probed path that sits on the volume.
    free_bytes
        Free bytes reported for that volume.

    """

    path: Path
    free_bytes: int


@dataclasses.dataclass(slots=True, frozen=True)
class ProbeError:
    """A volume that could not be measured.

    Attributes
    ----------
    path
        Probed path.
    error
        Failure raised by the probe.

    """

    path: Path
    error: OSError


@dataclasses.dataclass(slots=True, frozen=True)
class SpaceCheck:
    """Result of probing every path that must have free space.

    Attributes
    ----------
    low
        Volumes measured below the floor, in probe order.
    errors
        Probes that failed. A failure is not evidence of a full disk, so a
        caller reports it and carries on.

    """

    low: tuple[LowSpace, ...] = ()
    errors: tuple[ProbeError, ...] = ()


def resolve_temp_dir(
    environ: cabc.Mapping[str, str] | None = None,
) -> Path:
    """Return the temporary directory the build targets will write to.

    ``CLAUDE_CODE_TMPDIR`` wins over ``TMPDIR`` because Claude Code rewrites
    ``TMPDIR`` for its own scratch work while the hook's child processes see
    the Claude Code value. Blank values are ignored rather than becoming a
    relative path.

    Parameters
    ----------
    environ
        Environment mapping to read. Defaults to ``os.environ``.

    Returns
    -------
    Path
        Configured temporary directory, or the platform default.

    """
    env = os.environ if environ is None else environ
    for name in ("CLAUDE_CODE_TMPDIR", "TMPDIR"):
        value = env.get(name, "").strip()
        if value:
            return Path(value)
    return Path(tempfile.gettempdir())


def min_free_bytes(megabytes: int) -> int:
    """Convert a mebibyte threshold to bytes."""
    return megabytes * _BYTES_PER_MIB


def check_free_space(paths: cabc.Iterable[Path], *, minimum_bytes: int) -> SpaceCheck:
    """Measure each path's volume and report those below the floor.

    Repeated paths are probed once, so the same volume is never reported
    twice. A probe failure is collected rather than raised: the caller cannot
    conclude the disk is full from a path it could not read.

    Parameters
    ----------
    paths
        Paths to measure, in reporting order.
    minimum_bytes
        Strict free-space floor. A volume with exactly this much free space is
        not low.

    Returns
    -------
    SpaceCheck
        Low volumes and probe failures.

    """
    low: list[LowSpace] = []
    errors: list[ProbeError] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        try:
            free = shutil.disk_usage(path).free
        except OSError as exc:
            errors.append(ProbeError(path=path, error=exc))
            continue
        if free < minimum_bytes:
            low.append(LowSpace(path=path, free_bytes=free))
    return SpaceCheck(low=tuple(low), errors=tuple(errors))


def shows_disk_exhaustion(text: str) -> bool:
    """Return whether captured output reports a full disk."""
    return _DISK_EXHAUSTION.search(text) is not None


def format_bytes(size: int) -> str:
    """Render a byte count in binary units with one decimal place.

    Whole bytes stay whole: ``0 B`` reads better than ``0.0 B``. Sizes above
    the largest listed unit keep that unit, so the number stays readable
    without inventing a unit name.
    """
    if size < _BYTES_PER_KIB:
        return f"{size} B"
    value = float(size)
    for unit in _BINARY_UNITS:
        value /= _BYTES_PER_KIB
        if value < _BYTES_PER_KIB:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} {_BINARY_UNITS[-1]}"
