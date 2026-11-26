"""Exercise the Git upstream adapter's refresh and comparison decisions."""

from __future__ import annotations

import dataclasses
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
from pathlib import Path
from unittest import mock

import pytest

from post_turn_quality_stop_hook import branch_gates as branch_gates_mod
from post_turn_quality_stop_hook import process as process_mod
from post_turn_quality_stop_hook import state as state_mod
from post_turn_quality_stop_hook import upstream as upstream_mod
from post_turn_quality_stop_hook.config import Config
from post_turn_quality_stop_hook.git_facts import GitFacts

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
# the Git upstream adapter
# ---------------------------------------------------------------------------


class TestGitUpstreamHeadSource:
    """The Git adapter refreshes the upstream and compares it with HEAD."""

    @staticmethod
    def _facts(*, branch: str | None = "feature") -> GitFacts:
        """Return facts describing a branch tracking ``origin/feature``."""
        return GitFacts(
            primary_remote="origin",
            upstream_ref="origin/feature",
            pr_base_local_ref=None,
            local_base_commit=None,
            three_way_merge_is_configured=False,
            upstream_remote="origin",
            upstream_branch="feature",
            upstream_merge_ref="refs/heads/feature",
            branch=branch,
        )

    @staticmethod
    def _source(
        facts: GitFacts, *, refresh: bool = True
    ) -> upstream_mod.GitUpstreamHeadSource:
        """Build the adapter with the settings the hook would pass it."""
        return upstream_mod.GitUpstreamHeadSource(
            REPO,
            facts,
            refresh_upstream=refresh,
            fetch_timeout_seconds=10.0,
        )

    def test_a_bounded_fetch_with_invalid_utf8_passes_with_a_warning(
        self, tmp_path: Path
    ) -> None:
        """A bounded transport failure with bad bytes fails open, not raises."""
        script = (
            "import sys;sys.stderr.buffer.write(b'bad \\xff\\xfe bytes');sys.exit(1)"
        )
        seen_timeouts: list[float | None] = []

        def runner(
            cmd: list[str], _repo: Path, *, timeout: float | None = None
        ) -> subprocess.CompletedProcess[str]:
            """Run the fetch as a real child under the requested bound."""
            if cmd[1:2] == ["fetch"]:
                seen_timeouts.append(timeout)
                return process_mod.run(
                    [sys.executable, "-c", script], tmp_path, timeout=timeout
                )
            return _completed(0, stdout="head\n")

        facts = self._facts()
        source = upstream_mod.GitUpstreamHeadSource(
            REPO, facts, refresh_upstream=True, fetch_timeout_seconds=1.5
        )
        options = state_mod.StopCheckOptions(
            always_fetch=False, max_out=12000, config=Config()
        )
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("feature", None)
            ),
            # The preflight reads the local branch through the pipeline's own
            # import, so patch it there as well as in the adapter.
            mock.patch.object(
                branch_gates_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(upstream_mod, "run", side_effect=runner),
            mock.patch.object(
                upstream_mod, "remote_names", return_value=(["origin"], None)
            ),
        ):
            refresh = branch_gates_mod.refresh_upstream_head(
                REPO, facts, options, source
            )
            decision = branch_gates_mod.unpushed_commits_gate(refresh)

        assert seen_timeouts == [1.5], "the fetch must run under the configured timeout"
        assert isinstance(refresh, upstream_mod.UpstreamComparison), (
            "a failed fetch must still produce a comparison"
        )
        assert refresh.error is not None, "a failed fetch must report an error"
        assert "\ufffd" in str(refresh.error), (
            "undecodable bytes must be replaced and reported, not raised"
        )
        assert decision.outcome == "pass", "a failed refresh must fail open"
        assert decision.warning is not None, (
            "the gate must carry the failed-refresh warning its caller reports"
        )
        assert "could not verify the upstream" in decision.warning
        assert "\ufffd" in decision.warning, (
            "the gate must report the failed refresh with the replacement characters"
        )

    def test_remote_name_failure_is_reported_and_decision_is_unchanged(self) -> None:
        """A failed remote-name read warns at the boundary and still compares."""
        warnings: list[str] = []
        source = upstream_mod.GitUpstreamHeadSource(
            REPO,
            self._facts(),
            refresh_upstream=True,
            fetch_timeout_seconds=10.0,
            on_warning=warnings.append,
        )
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod, "run", return_value=_completed(0, stdout="head\n")
            ),
            mock.patch.object(
                upstream_mod,
                "remote_names",
                return_value=(None, "git remote failed: offline"),
            ) as read_names,
            mock.patch.object(
                upstream_mod,
                "fetch_upstream_head",
                return_value=("upstream-head", None),
            ),
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(True, None)
            ),
        ):
            comparison = source.refresh_comparison()

        read_names.assert_called_once_with(REPO)
        assert comparison == upstream_mod.UpstreamComparison(
            remote="origin", branch="feature", remote_names=None, ahead=True
        ), f"unexpected comparison {comparison!r}"
        assert any("could not read remote names" in warning for warning in warnings), (
            f"a failed remote-name read must be reported, got {warnings!r}"
        )

    def test_refuses_to_compare_after_the_checkout_moves(self) -> None:
        """A branch change after fact collection must not count as unpushed."""
        source = self._source(self._facts())
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("other", None)
            ),
            mock.patch.object(
                upstream_mod, "run", return_value=_completed(0, stdout="head\n")
            ),
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            comparison = source.refresh_comparison()

        fetch.assert_not_called()
        ahead.assert_not_called()
        assert isinstance(comparison, upstream_mod.UpstreamComparison), (
            f"a branch change must still produce a comparison, got {comparison!r}"
        )
        assert comparison.ahead is None, (
            "a branch change after fact collection must prevent an ahead result"
        )
        assert comparison.error is not None, (
            "a branch change must be reported as an error"
        )
        assert "moved from feature to other" in str(comparison.error), (
            f"the move must be named in the error, got {comparison.error!r}"
        )

    def test_detached_head_after_the_checkout_moves_is_reported(self) -> None:
        """A move to a detached HEAD names that state in the error."""
        source = self._source(self._facts())
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=(None, None)
            ),
            mock.patch.object(
                upstream_mod, "run", return_value=_completed(0, stdout="head\n")
            ),
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
        ):
            comparison = source.refresh_comparison()

        fetch.assert_not_called()
        assert isinstance(comparison, upstream_mod.UpstreamComparison), (
            f"a detached checkout must still produce a comparison, got {comparison!r}"
        )
        assert comparison.error is not None, (
            "a move to a detached HEAD must be reported as an error"
        )
        assert "moved from feature to a detached HEAD" in str(comparison.error), (
            f"the move must be named in the error, got {comparison.error!r}"
        )

    def test_refuses_to_compare_when_head_changes_during_the_fetch(self) -> None:
        """Two different HEAD readings around the fetch invalidate the check."""
        source = self._source(self._facts())
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod,
                "run",
                side_effect=[
                    _completed(0, stdout="first\n"),
                    _completed(0, stdout="second\n"),
                ],
            ),
            mock.patch.object(
                upstream_mod,
                "fetch_upstream_head",
                return_value=("upstream-head", None),
            ),
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            comparison = source.refresh_comparison()

        ahead.assert_not_called()
        assert isinstance(comparison, upstream_mod.UpstreamComparison), (
            f"a moved checkout must still produce a comparison, got {comparison!r}"
        )
        assert comparison.ahead is None, (
            "checkout movement during the fetch must prevent an ahead result"
        )
        assert comparison.error is not None
        assert comparison.error.message == (
            "the checkout changed while the upstream head was fetched"
        ), f"the movement must be reported as the reason, got {comparison.error!r}"

    def test_local_upstream_never_fetches(self) -> None:
        """A ``.`` upstream names a local branch, which needs no transport."""
        facts = dataclasses.replace(
            self._facts(),
            upstream_remote=".",
            upstream_merge_ref="refs/heads/integration",
            upstream_branch="integration",
        )
        source = self._source(facts)
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod, "run", return_value=_completed(0, stdout="head\n")
            ),
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(False, None)
            ) as ahead,
        ):
            comparison = source.refresh_comparison()

        fetch.assert_not_called()
        ahead.assert_called_once_with(REPO, "refs/heads/integration")
        assert isinstance(comparison, upstream_mod.UpstreamComparison), (
            f"a local upstream must still produce a comparison, got {comparison!r}"
        )
        assert comparison.ahead is False, (
            f"HEAD equal to the local upstream must not be ahead, got "
            f"{comparison.ahead!r}"
        )

    def test_disabled_refresh_uses_the_tracking_ref(self) -> None:
        """The opt-out reads the local tracking ref without any transport."""
        source = self._source(self._facts(), refresh=False)
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod, "run", return_value=_completed(0, stdout="head\n")
            ),
            mock.patch.object(upstream_mod, "fetch_upstream_head") as fetch,
            mock.patch.object(
                upstream_mod, "has_unpushed_commits", return_value=(True, None)
            ) as ahead,
        ):
            comparison = source.refresh_comparison()

        fetch.assert_not_called()
        ahead.assert_called_once_with(REPO, "origin/feature")
        assert isinstance(comparison, upstream_mod.UpstreamComparison), (
            f"the opt-out must still produce a comparison, got {comparison!r}"
        )
        assert comparison.ahead is True, (
            f"the opt-out must report the tracking ref's ahead result, got "
            f"{comparison.ahead!r}"
        )

    @pytest.mark.parametrize(
        "upstream_remote",
        [
            # A configured remote name and an explicit URL both lack a local
            # tracking ref here, so both must fail open the same way.
            "origin",
            "https://example.invalid/repo.git",
        ],
    )
    def test_disabled_refresh_without_a_local_ref_reports_an_error(
        self, upstream_remote: str
    ) -> None:
        """An upstream with no local ref makes the adapter fail open."""
        facts = dataclasses.replace(
            self._facts(), upstream_ref=None, upstream_remote=upstream_remote
        )
        source = self._source(facts, refresh=False)
        with (
            mock.patch.object(
                upstream_mod, "current_branch", return_value=("feature", None)
            ),
            mock.patch.object(
                upstream_mod, "run", return_value=_completed(0, stdout="head\n")
            ),
            mock.patch.object(upstream_mod, "has_unpushed_commits") as ahead,
        ):
            comparison = source.refresh_comparison()

        ahead.assert_not_called()
        assert isinstance(comparison, upstream_mod.UpstreamComparison), (
            f"a missing local ref must still produce a comparison, got {comparison!r}"
        )
        assert comparison.error is not None
        assert comparison.error.message == (
            "upstream refresh is disabled and the upstream is not a local ref"
        ), (
            f"a missing local ref must fail open for {upstream_remote!r}, got "
            f"{comparison.error!r}"
        )
