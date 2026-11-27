"""Exercise cleanup of the temporary ref used to hold a fetched head."""

from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path
from unittest import mock

from post_turn_quality_stop_hook import upstream as upstream_mod


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess with the given return code and output."""
    return subprocess.CompletedProcess(
        args=["unit-test"], returncode=returncode, stdout=stdout, stderr=stderr
    )


REPO = Path("/fake/repo")


class TestTempRefCleanup:
    """The private fetch ref is deleted best effort, and failures are logged."""

    def test_delete_temp_ref_failure_is_reported_with_the_ref_name(self) -> None:
        """A rejected deletion is reported for cleanup and keeps the result."""
        warnings: list[str] = []
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"

        def runner(
            cmd: list[str], _repo: Path, *, timeout: float | None = None
        ) -> subprocess.CompletedProcess[str]:
            """Answer the fetch and rev-parse, and reject the deletion."""
            del timeout
            if cmd[1:2] == ["fetch"]:
                return _completed(0)
            if cmd[1:2] == ["rev-parse"]:
                return _completed(0, stdout="abc123\n")
            return _completed(1, stderr="fatal: reference transaction rejected")

        head, err = upstream_mod.fetch_upstream_head(
            REPO,
            "origin",
            "feature",
            options=upstream_mod.FetchOptions(
                runner=runner,
                temp_ref_factory=lambda: temp_ref,
                on_warning=warnings.append,
            ),
        )

        assert (head, err) == ("abc123", None), (
            "a failed cleanup must not invalidate a successful comparison"
        )
        assert any(
            temp_ref in message and "reference transaction rejected" in message
            for message in warnings
        ), "a failed deletion must name the leftover ref and its reason"

    def test_delete_temp_ref_withholds_unsafe_cleanup_diagnostics(self) -> None:
        """A hook's unsafe diagnostic is replaced, but the ref name is kept."""
        warnings: list[str] = []
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        malformed = "https://user:pa/ss@example.invalid/repo.git"
        with mock.patch.object(
            upstream_mod,
            "run",
            return_value=_completed(1, stderr=f"fatal: hook saw {malformed}"),
        ):
            upstream_mod._delete_temp_ref(REPO, temp_ref, on_warning=warnings.append)

        messages = warnings
        assert any(temp_ref in message for message in messages), (
            f"a failed deletion must name the leftover ref, got {messages!r}"
        )
        assert any("could not be shown safely" in message for message in messages), (
            f"withheld diagnostics must say so, got {messages!r}"
        )
        assert not any("pa/ss" in message for message in messages), (
            "a credential fragment must not reach the log"
        )
        assert not any(malformed in message for message in messages), (
            "the unsafe URL must not reach the log"
        )

    def test_delete_temp_ref_bounds_the_cleanup_command(self) -> None:
        """The ref deletion is bounded so a stalled hook cannot hang the gate."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        with mock.patch.object(
            upstream_mod, "run", return_value=_completed(124, stderr="timed out")
        ) as mock_run:
            upstream_mod._delete_temp_ref(
                REPO, temp_ref, on_warning=lambda _message: None
            )
        mock_run.assert_called_once_with(
            ["git", "update-ref", "-d", temp_ref],
            REPO,
            timeout=upstream_mod._REF_CLEANUP_TIMEOUT_SECONDS,
        )
