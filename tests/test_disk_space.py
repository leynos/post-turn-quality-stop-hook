"""Exercise the free-space policy used by the stop hook."""

from __future__ import annotations

import types
from pathlib import Path
from unittest import mock

import pytest

from post_turn_quality_stop_hook import disk_space as disk_mod


def _usage(free: int) -> types.SimpleNamespace:
    """Build the part of ``shutil.disk_usage``'s result the policy reads."""
    return types.SimpleNamespace(total=free, used=0, free=free)


# ---------------------------------------------------------------------------
# resolve_temp_dir
# ---------------------------------------------------------------------------


class TestResolveTempDir:
    """Tests for resolve_temp_dir()."""

    def test_claude_code_tmpdir_wins_over_tmpdir(self) -> None:
        """Claude Code's temporary directory takes precedence."""
        environ = {"CLAUDE_CODE_TMPDIR": "/claude/tmp", "TMPDIR": "/configured/scratch"}

        assert disk_mod.resolve_temp_dir(environ) == Path("/claude/tmp")

    def test_tmpdir_used_when_claude_code_value_is_blank(self) -> None:
        """A whitespace-only Claude Code value falls through to TMPDIR."""
        environ = {"CLAUDE_CODE_TMPDIR": "   ", "TMPDIR": "/configured/runtime"}

        assert disk_mod.resolve_temp_dir(environ) == Path("/configured/runtime")

    @pytest.mark.parametrize("value", ["", "  "], ids=["empty", "blank"])
    def test_blank_overrides_fall_through_to_the_platform_default(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Blank values never become a relative temporary directory."""
        monkeypatch.setattr(disk_mod.tempfile, "gettempdir", lambda: "/platform")

        environ = {"CLAUDE_CODE_TMPDIR": value, "TMPDIR": value}

        assert disk_mod.resolve_temp_dir(environ) == Path("/platform")

    def test_empty_mapping_uses_the_platform_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An isolated empty mapping never observes the process environment."""
        monkeypatch.setattr(disk_mod.tempfile, "gettempdir", lambda: "/platform")

        assert disk_mod.resolve_temp_dir({}) == Path("/platform")

    def test_absent_mapping_reads_the_process_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Omitting the mapping reads ``os.environ``."""
        monkeypatch.setenv("CLAUDE_CODE_TMPDIR", "/from/process")

        assert disk_mod.resolve_temp_dir() == Path("/from/process")


# ---------------------------------------------------------------------------
# min_free_bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("megabytes", "expected"),
    [
        (0, 0),
        (1, 1024 * 1024),
        (100, 100 * 1024 * 1024),
    ],
)
def test_min_free_bytes_converts_mebibytes(megabytes: int, expected: int) -> None:
    """The configured threshold is a mebibyte count converted to bytes."""
    assert disk_mod.min_free_bytes(megabytes) == expected


# ---------------------------------------------------------------------------
# check_free_space
# ---------------------------------------------------------------------------


class TestCheckFreeSpace:
    """Tests for check_free_space()."""

    def test_paths_above_the_threshold_report_no_low_space(self) -> None:
        """Ample volumes produce neither low-space findings nor errors."""
        usage = mock.Mock(return_value=_usage(2000))
        with mock.patch.object(disk_mod.shutil, "disk_usage", usage):
            result = disk_mod.check_free_space(
                [Path("/repo"), Path("/scratch")], minimum_bytes=1000
            )

        assert result == disk_mod.SpaceCheck(), (
            f"expected a clear space check but got {result!r}"
        )
        assert [call.args for call in usage.call_args_list] == [
            (Path("/repo"),),
            (Path("/scratch"),),
        ], f"expected both paths probed in order but got {usage.call_args_list!r}"

    def test_free_space_equal_to_the_threshold_is_not_low(self) -> None:
        """The threshold is a strict lower bound, not an inclusive one."""
        with mock.patch.object(
            disk_mod.shutil, "disk_usage", return_value=_usage(1000)
        ):
            result = disk_mod.check_free_space([Path("/repo")], minimum_bytes=1000)

        assert result.low == (), f"expected no low space but got {result.low!r}"

    def test_free_space_below_the_threshold_is_low(self) -> None:
        """One byte below the threshold is reported with its measurement."""
        with mock.patch.object(disk_mod.shutil, "disk_usage", return_value=_usage(999)):
            result = disk_mod.check_free_space([Path("/repo")], minimum_bytes=1000)

        assert result.low == (disk_mod.LowSpace(path=Path("/repo"), free_bytes=999),), (
            f"expected the volume to be low but got {result.low!r}"
        )

    def test_duplicate_paths_are_probed_once(self) -> None:
        """A repeated path does not produce a second probe or finding."""
        usage = mock.Mock(return_value=_usage(10))
        with mock.patch.object(disk_mod.shutil, "disk_usage", usage):
            result = disk_mod.check_free_space(
                [Path("/repo"), Path("/repo"), Path("/repo")], minimum_bytes=1000
            )

        assert usage.call_count == 1, (
            f"expected one probe for the repeated path but got {usage.call_count}"
        )
        assert result.low == (disk_mod.LowSpace(path=Path("/repo"), free_bytes=10),), (
            f"expected one low-space finding but got {result.low!r}"
        )

    def test_probe_errors_are_returned_separately(self) -> None:
        """A failed probe is reported with its path and never raised."""
        error = OSError("No such file or directory")
        with mock.patch.object(
            disk_mod.shutil, "disk_usage", side_effect=[error, _usage(10)]
        ):
            result = disk_mod.check_free_space(
                [Path("/missing"), Path("/scratch")], minimum_bytes=100
            )

        assert result.errors == (
            disk_mod.ProbeError(path=Path("/missing"), error=error),
        ), f"expected the probe error to be returned but got {result.errors!r}"
        expected = (disk_mod.LowSpace(path=Path("/scratch"), free_bytes=10),)
        assert result.low == expected, (
            f"expected the remaining path to be measured but got {result.low!r}"
        )

    def test_low_space_findings_keep_probe_order(self) -> None:
        """Every low volume is reported in the order it was probed."""
        with mock.patch.object(
            disk_mod.shutil,
            "disk_usage",
            side_effect=[_usage(1), _usage(2)],
        ):
            result = disk_mod.check_free_space(
                [Path("/first"), Path("/second")], minimum_bytes=10
            )

        assert result.low == (
            disk_mod.LowSpace(path=Path("/first"), free_bytes=1),
            disk_mod.LowSpace(path=Path("/second"), free_bytes=2),
        ), f"expected both volumes in probe order but got {result.low!r}"


# ---------------------------------------------------------------------------
# shows_disk_exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "No space left on device",
        "write failed: no space left on device",
        "OSError: [Errno 28] No space left on device",
        "error: ENOSPC",
        "failed with enospc while writing",
        "rustc: os error 28",
        "OS error 28",
    ],
)
def test_shows_disk_exhaustion_recognises_known_messages(text: str) -> None:
    """Each documented disk-exhaustion spelling is recognised."""
    assert disk_mod.shows_disk_exhaustion(text), (
        f"expected {text!r} to report disk exhaustion"
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "compilation failed",
        "not enough disk space",
        "MYENOSPC",
        "ENOSPCX",
        "os error 280",
    ],
)
def test_shows_disk_exhaustion_ignores_other_failures(text: str) -> None:
    """Ordinary failures and near-misses never short-circuit the stop."""
    assert not disk_mod.shows_disk_exhaustion(text), (
        f"expected {text!r} not to report disk exhaustion"
    )


# ---------------------------------------------------------------------------
# format_bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024**2, "1.0 MiB"),
        (1024**3, "1.0 GiB"),
        (1024**4, "1.0 TiB"),
        (1024**5, "1024.0 TiB"),
    ],
)
def test_format_bytes_uses_binary_units(size: int, expected: str) -> None:
    """Byte counts are readable without losing the binary scale."""
    assert disk_mod.format_bytes(size) == expected
