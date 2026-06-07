"""GitHub pull request lookup for the stop hook."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import os
import re
import shutil
import subprocess  # noqa: S404
import typing as typ

import github3

if typ.TYPE_CHECKING:
    import collections.abc as cabc


REMOTE_RE = re.compile(
    r"^(?:git@(?P<ssh_host>[^:]+):|https://(?P<https_host>[^/]+)/)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


@dataclasses.dataclass(slots=True, frozen=True)
class PullRequestSummary:
    """Summary of the open pull request for the current branch.

    Attributes
    ----------
    number
        Pull request number.
    base_branch
        Target branch name for the pull request.
    base_oid
        Base branch commit OID reported by GitHub.

    """

    number: int
    base_branch: str
    base_oid: str


def lookup_pr(
    remote_url: str, branch: str, *, timeout: float
) -> PullRequestSummary | None:
    """Look up the first open pull request for ``branch`` on ``remote_url``.

    Parameters
    ----------
    remote_url
        Git remote URL to query.
    branch
        Branch name to find an open pull request for.
    timeout
        Seconds to wait for the network lookup.

    Returns
    -------
    PullRequestSummary | None
        Pull request summary on success, otherwise ``None`` for invalid remote
        URLs, missing tokens, lookup failures, or timeouts.

    Notes
    -----
    ``lookup_pr`` uses ``parse_remote_url`` and ``github_token`` to validate
    inputs, then runs ``_lookup_pr`` in a ``ThreadPoolExecutor``.

    """
    parsed = parse_remote_url(remote_url)
    if parsed is None:
        return None

    token = github_token()
    if token is None:
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_lookup_pr, parsed, branch, token)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return None


def parse_remote_url(remote_url: str) -> tuple[str, str] | None:
    """Parse SSH or HTTPS GitHub remote URLs into owner and repository name.

    Parameters
    ----------
    remote_url
        GitHub SSH or HTTPS remote URL.

    Returns
    -------
    tuple[str, str] | None
        ``(owner, repo)`` when ``REMOTE_RE`` matches, otherwise ``None``.

    """
    match = REMOTE_RE.match(remote_url)
    if match is None:
        return None
    return match.group("owner"), match.group("repo")


def github_token() -> str | None:
    """Return a GitHub token from the environment or ``gh auth token``.

    Returns
    -------
    str | None
        Token from ``GITHUB_TOKEN`` first, then from ``gh auth token`` when
        ``gh`` is available. Returns ``None`` when ``gh`` is missing, the
        subprocess fails, or the token output is empty.

    Notes
    -----
    ``github_token`` handles ``FileNotFoundError`` defensively even though the
    executable path is resolved with ``shutil.which``.

    """
    env_token = os.environ.get("GITHUB_TOKEN")
    if env_token:
        return env_token

    gh_bin = shutil.which("gh")
    if gh_bin is None:
        return None

    try:
        token = subprocess.run(  # noqa: S603  # command argv is fixed and read-only.
            [gh_bin, "auth", "token"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if token.returncode != 0:
        return None
    stripped = token.stdout.strip()
    return stripped or None


def _lookup_pr(
    repo_id: tuple[str, str], branch: str, token: str
) -> PullRequestSummary | None:
    owner, repo_name = repo_id
    client = github3.login(token=token)
    repository = client.repository(owner, repo_name)
    pulls = typ.cast(
        "cabc.Iterator[typ.Any]",
        repository.pull_requests(state="open", head=f"{owner}:{branch}"),
    )
    pull = next(pulls, None)
    if pull is None:
        return None
    return PullRequestSummary(
        number=int(pull.number),
        base_branch=str(pull.base.ref),
        base_oid=str(pull.base.sha),
    )
