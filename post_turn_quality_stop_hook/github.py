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
    """Summary of the open pull request for the current branch."""

    number: int
    base_branch: str
    base_oid: str


def lookup_pr(
    remote_url: str, branch: str, *, timeout: float
) -> PullRequestSummary | None:
    """Look up the first open pull request for ``branch`` on ``remote_url``."""
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
    """Parse SSH or HTTPS GitHub remote URLs into owner and repository name."""
    match = REMOTE_RE.match(remote_url)
    if match is None:
        return None
    return match.group("owner"), match.group("repo")


def github_token() -> str | None:
    """Return a GitHub token from the environment or `gh auth token`."""
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
