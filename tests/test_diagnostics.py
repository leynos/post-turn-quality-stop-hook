"""Warnings stay on standard error and never escape as exceptions.

Standard output carries the JSON decision, so a missing or broken standard
error must cost a warning at most. ``print(file=None)`` would write to standard
output, which is why :func:`warn` checks for a stream before writing.
"""

from __future__ import annotations

import io
import sys

import pytest

from post_turn_quality_stop_hook.diagnostics import warn


def test_warning_is_prefixed_on_standard_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A working standard error receives the fixed prefix and message."""
    warn("could not create the decision log")

    captured = capsys.readouterr()
    assert captured.err == (
        "post-turn-quality-stop-hook: could not create the decision log\n"
    )
    assert captured.out == "", "a warning must never reach standard output"


def test_missing_standard_error_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A process without standard error still writes nothing to standard out."""
    monkeypatch.setattr(sys, "stderr", None)

    warn("could not create the decision log")

    assert capsys.readouterr().out == "", (
        "a missing standard error must not redirect the warning to standard out"
    )


def test_closed_standard_error_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A closed stream raises ``ValueError`` from ``print``, which is swallowed."""
    stream = io.StringIO()
    stream.close()
    monkeypatch.setattr(sys, "stderr", stream)

    warn("could not create the decision log")

    assert capsys.readouterr().out == ""


def test_broken_standard_error_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stream that rejects writes cannot fail the hook."""
    stream = io.StringIO()

    def refuse(_text: str) -> int:
        """Fail the write as a broken pipe would."""
        message = "broken pipe"
        raise OSError(message)

    monkeypatch.setattr(stream, "write", refuse)
    monkeypatch.setattr(sys, "stderr", stream)

    warn("could not create the decision log")

    assert capsys.readouterr().out == ""
