"""Exercise the upstream fetch and the Git upstream adapter."""

from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
from pathlib import Path
from unittest import mock

import pytest

from post_turn_quality_stop_hook import process as process_mod
from post_turn_quality_stop_hook import upstream as upstream_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess with the given return code and output."""
    return subprocess.CompletedProcess(
        args=["unit-test"], returncode=returncode, stdout=stdout, stderr=stderr
    )


REPO = Path("/fake/repo")

# ---------------------------------------------------------------------------
# fetch_upstream_head
# ---------------------------------------------------------------------------


class TestFetchUpstreamHead:
    """Tests for fetch_upstream_head()."""

    def test_returns_fetched_head(self) -> None:
        """The fetched commit is read from the temporary ref and then removed."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        with (
            mock.patch.object(
                upstream_mod.uuid, "uuid4", return_value=mock.Mock(hex="deadbeef")
            ),
            mock.patch.object(upstream_mod, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(0),
                _completed(0, stdout="abc123\n"),
                _completed(0),
            ]
            head, err = upstream_mod.fetch_upstream_head(
                REPO,
                "origin",
                "feature",
                options=upstream_mod.FetchOptions(timeout=5.0),
            )
        assert head == "abc123", f"expected abc123 but got {head!r}"
        assert err is None, f"expected no error but got {err!r}"
        assert mock_run.call_args_list == [
            mock.call(
                [
                    "git",
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--no-recurse-submodules",
                    "--refmap=",
                    "--",
                    "origin",
                    f"+refs/heads/feature:{temp_ref}",
                ],
                REPO,
                timeout=5.0,
            ),
            mock.call(["git", "rev-parse", "--verify", f"{temp_ref}^{{commit}}"], REPO),
            mock.call(
                ["git", "update-ref", "-d", temp_ref],
                REPO,
                timeout=upstream_mod._REF_CLEANUP_TIMEOUT_SECONDS,
            ),
        ]

    def test_uses_injected_runner_and_temp_ref_name(self) -> None:
        """The fetch and cleanup use the injected runner and ref name."""
        seen: list[tuple[list[str], float | None]] = []
        temp_ref = "refs/post-turn-quality/fetch/injected"

        def runner(
            cmd: list[str], _repo: Path, *, timeout: float | None = None
        ) -> subprocess.CompletedProcess[str]:
            """Record each command and answer fetch and rev-parse."""
            seen.append((cmd, timeout))
            if cmd[1:2] == ["fetch"]:
                return _completed(0)
            if cmd[1:2] == ["rev-parse"]:
                return _completed(0, stdout="abc123\n")
            return _completed(0)

        head, err = upstream_mod.fetch_upstream_head(
            REPO,
            "origin",
            "feature",
            options=upstream_mod.FetchOptions(
                runner=runner,
                temp_ref_factory=lambda: temp_ref,
            ),
        )

        assert (head, err) == ("abc123", None)
        assert seen[0][0] == [
            "git",
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            "--no-recurse-submodules",
            "--refmap=",
            "--",
            "origin",
            f"+refs/heads/feature:{temp_ref}",
        ]
        assert seen[-1] == (
            ["git", "update-ref", "-d", temp_ref],
            upstream_mod._REF_CLEANUP_TIMEOUT_SECONDS,
        )

    def test_preserves_a_non_branch_upstream_ref(self) -> None:
        """A configured ref outside refs/heads is fetched unchanged."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        with (
            mock.patch.object(
                upstream_mod.uuid, "uuid4", return_value=mock.Mock(hex="deadbeef")
            ),
            mock.patch.object(upstream_mod, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(0),
                _completed(0, stdout="abc123\n"),
                _completed(0),
            ]
            head, err = upstream_mod.fetch_upstream_head(
                REPO, "origin", "refs/merge-requests/1/head"
            )
        assert (head, err) == ("abc123", None)
        assert mock_run.call_args_list[0] == mock.call(
            [
                "git",
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "--no-recurse-submodules",
                "--refmap=",
                "--",
                "origin",
                f"+refs/merge-requests/1/head:{temp_ref}",
            ],
            REPO,
            timeout=None,
        )

    def test_fetch_failure_returns_error_and_cleans_up(self) -> None:
        """A failed fetch reports its error and deletes the temporary ref."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        with (
            mock.patch.object(
                upstream_mod.uuid, "uuid4", return_value=mock.Mock(hex="deadbeef")
            ),
            mock.patch.object(upstream_mod, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(128, stderr="fatal: unreachable"),
                _completed(0),
            ]
            head, err = upstream_mod.fetch_upstream_head(REPO, "origin", "feature")
        assert head is None, f"expected no head but got {head!r}"
        assert "git fetch origin feature failed" in (err or ""), (
            f"expected fetch failure message but got {err!r}"
        )
        assert "fatal: unreachable" in (err or ""), (
            f"expected git error in message but got {err!r}"
        )
        assert mock_run.call_args_list[-1] == mock.call(
            ["git", "update-ref", "-d", temp_ref],
            REPO,
            timeout=upstream_mod._REF_CLEANUP_TIMEOUT_SECONDS,
        )

    @pytest.mark.parametrize(
        ("diagnostic", "expected"),
        [
            (
                "fatal: could not read from https://user:secret@example.invalid/repo.git",
                "fatal: could not read from https://example.invalid/repo.git",
            ),
            (
                # Git can shorten the URL to the authority only, which does
                # not match the configured remote verbatim.
                "fatal: authentication failed for https://token@example.invalid",
                "fatal: authentication failed for https://example.invalid",
            ),
            (
                # A raw ``@`` inside the password must not survive redaction.
                "fatal: could not read from https://user:p@ss@example.invalid/repo.git",
                "fatal: could not read from https://example.invalid/repo.git",
            ),
        ],
        ids=["verbatim-echo", "shortened-url", "raw-at-in-password"],
    )
    def test_fetch_failure_redacts_url_credentials(
        self, diagnostic: str, expected: str
    ) -> None:
        """URL userinfo is removed wherever Git repeats it in a diagnostic."""
        remote = "https://user:secret@example.invalid/repo.git"
        with (
            mock.patch.object(
                upstream_mod.uuid, "uuid4", return_value=mock.Mock(hex="deadbeef")
            ),
            mock.patch.object(upstream_mod, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(128, stderr=diagnostic),
                _completed(0),
            ]
            head, err = upstream_mod.fetch_upstream_head(REPO, remote, "feature")
        assert head is None, f"expected no head but got {head!r}"
        assert "secret" not in (err or ""), f"credentials leaked in {err!r}"
        assert "token@" not in (err or ""), f"credentials leaked in {err!r}"
        assert expected in (err or ""), f"expected {expected!r} in {err!r}"

    def test_rev_parse_failure_returns_error_and_cleans_up(self) -> None:
        """A failed rev-parse reports its error and deletes the temporary ref."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        with (
            mock.patch.object(
                upstream_mod.uuid, "uuid4", return_value=mock.Mock(hex="deadbeef")
            ),
            mock.patch.object(upstream_mod, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(0),
                _completed(128, stderr="fatal: bad revision"),
                _completed(0),
            ]
            head, err = upstream_mod.fetch_upstream_head(REPO, "origin", "feature")
        assert head is None, f"expected no head but got {head!r}"
        assert "git rev-parse feature failed" in (err or ""), (
            f"expected rev-parse failure message but got {err!r}"
        )
        assert mock_run.call_args_list[-1] == mock.call(
            ["git", "update-ref", "-d", temp_ref],
            REPO,
            timeout=upstream_mod._REF_CLEANUP_TIMEOUT_SECONDS,
        )

    def test_empty_rev_parse_output_returns_error(self) -> None:
        """Empty rev-parse output is an error rather than a commit."""
        with (
            mock.patch.object(
                upstream_mod.uuid, "uuid4", return_value=mock.Mock(hex="deadbeef")
            ),
            mock.patch.object(upstream_mod, "run") as mock_run,
        ):
            mock_run.side_effect = [
                _completed(0),
                _completed(0, stdout="\n"),
                _completed(0),
            ]
            head, err = upstream_mod.fetch_upstream_head(REPO, "origin", "feature")
        assert head is None, f"expected no head but got {head!r}"
        assert "empty output" in (err or ""), (
            f"expected empty output error but got {err!r}"
        )

    def test_invalid_utf8_fetch_diagnostics_become_a_returned_error(
        self, tmp_path: Path
    ) -> None:
        """A transport writing non-UTF-8 stderr returns an error, not a raise."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        script = (
            "import sys;sys.stderr.buffer.write(b'bad \\xff\\xfe bytes');sys.exit(1)"
        )

        def runner(
            cmd: list[str], _repo: Path, *, timeout: float | None = None
        ) -> subprocess.CompletedProcess[str]:
            """Run a command that fails with non-UTF-8 diagnostics."""
            if cmd[1:2] == ["fetch"]:
                return process_mod.run(
                    [sys.executable, "-c", script], tmp_path, timeout=timeout
                )
            return _completed(0)

        head, err = upstream_mod.fetch_upstream_head(
            REPO,
            "origin",
            "feature",
            options=upstream_mod.FetchOptions(
                runner=runner, temp_ref_factory=lambda: temp_ref
            ),
        )

        assert head is None, f"a failed fetch must return no head, got {head!r}"
        assert err is not None, "a failed fetch must return an error"
        assert "git fetch origin feature failed" in err, (
            f"the error must name the fetch that failed, got {err!r}"
        )
        assert "\ufffd" in err, "undecodable bytes must be replaced rather than raised"

    def test_a_fetch_that_cannot_start_returns_an_error(self) -> None:
        """A runner that raises OSError must not escape as an exception."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/deadbeef"
        deletions: list[list[str]] = []
        launch_error = "git is not executable"

        def runner(
            cmd: list[str], _repo: Path, *, timeout: float | None = None
        ) -> subprocess.CompletedProcess[str]:
            """Fail the transport launch, and record the cleanup command."""
            del timeout
            if cmd[1:2] == ["fetch"]:
                raise PermissionError(launch_error)
            deletions.append(cmd)
            return _completed(0)

        head, err = upstream_mod.fetch_upstream_head(
            REPO,
            "origin",
            "feature",
            options=upstream_mod.FetchOptions(
                runner=runner, temp_ref_factory=lambda: temp_ref
            ),
        )

        assert head is None, (
            f"a fetch that could not run must return no head, got {head!r}"
        )
        assert err is not None, "a fetch that could not run must return an error"
        assert "git fetch origin feature could not run" in err, (
            f"the error must name the fetch that could not run, got {err!r}"
        )
        assert "git is not executable" in err, (
            f"the cause must be reported, got {err!r}"
        )
        assert deletions == [["git", "update-ref", "-d", temp_ref]], (
            "the temporary ref must still be cleaned up after a launch failure"
        )

    def test_named_remote_withholds_malformed_credential_diagnostics(self) -> None:
        """A named remote can still make Git repeat an unsafe URL in an error."""
        temp_ref = f"{upstream_mod._TEMP_FETCH_REF_PREFIX}/beefcafe"
        malformed = "https://user:pa/ss@example.invalid/repo.git"

        def runner(
            cmd: list[str], _repo: Path, *, timeout: float | None = None
        ) -> subprocess.CompletedProcess[str]:
            """Fail the fetch with a diagnostic that repeats the unsafe URL."""
            if cmd[1:2] == ["fetch"]:
                return _completed(1, stderr=f"fatal: unable to access '{malformed}/'")
            return _completed(0)

        head, err = upstream_mod.fetch_upstream_head(
            REPO,
            "origin",
            "feature",
            options=upstream_mod.FetchOptions(
                runner=runner, temp_ref_factory=lambda: temp_ref
            ),
        )

        assert head is None, f"a failed fetch must return no head, got {head!r}"
        assert err is not None, "a failed fetch must return an error"
        assert "git fetch origin feature failed" in err, (
            "the error must still name the fetch that failed"
        )
        assert "pa/ss" not in err, "a credential fragment must not reach the error"
        assert malformed not in err, "the unsafe URL must not reach the error"
        assert "could not be shown safely" in err, (
            "withheld diagnostics must say so rather than look empty"
        )
