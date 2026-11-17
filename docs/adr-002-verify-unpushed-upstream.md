# Architectural decision record (ADR) 002: Verify the unpushed upstream head

## Status

Accepted on 2026-11-17. The unpushed-commits gate refreshes the configured
upstream into a private ref and compares `HEAD` with the fetched commit,
failing open when the remote cannot be verified.

## Date

2026-11-17.

## Context and problem statement

The stop hook blocks when the current branch has commits the upstream does not.
Historically it compared `HEAD` against the local remote-tracking ref, such as
`origin/feature`. That ref is a cache. An explicit-URL push moves the remote
branch without updating the remote-tracking ref, so the hook can block after a
successful push. The inverse failure also occurs when the upstream is
configured as a URL rather than a remote name: Git cannot store a URL as an
upstream, so `@{u}` resolution fails and the gate silently skipped instead of
checking the commits.

The gate needs an authoritative comparison point for both remote names and
URL-valued upstreams, without turning a transient network failure into a false
block.

## Decision Drivers

- A push to an explicit URL must not leave a stale local ref in control.
- A URL-valued upstream must still be checked rather than silently skipped.
- The hook runs on every stop, so the refresh must be bounded.
- Network and remote availability are not under the hook's control.
- Remote URLs can contain credentials that must never reach output or logs.

## Decision outcome

Resolve the upstream from `branch.<name>.remote` and the first
`branch.<name>.merge` value rather than relying on `@{u}`. Keep the merge ref
fully qualified so a tag that shares the branch name cannot shadow the branch.

Refresh the upstream with `git fetch` into a unique private ref under
`refs/post-turn-quality/fetch/`, read that ref, then delete it. A unique ref
isolates the result from a concurrent fetch that would replace the shared
`FETCH_HEAD`, and `--no-write-fetch-head` avoids disturbing it. The fetch and
the temporary-ref deletion are bounded by timeouts and run within a process
group so a transport such as SSH cannot outlive the deadline. The ref-name
source and command runner are injectable for tests.

Compare `HEAD` with the fetched commit. When resolution or the fetch fails, log
the reason and fail open: an unverifiable remote must not block a turn.
Credentials in a URL are redacted from every returned error.

The pipeline separates the command step (`refresh_upstream_head`) from the
decision function (`unpushed_commits_gate`). The decision function consumes the
collected comparison and performs no network access or ref writes; only the
command step performs I/O.

## Alternatives considered

- **Continue using the remote-tracking ref.** Rejected: it is a cache and is
  stale after an explicit-URL push, which is the reported failure mode.
- **Use `git ls-remote`.** Rejected: it reports a commit identifier but does not
  make the object available locally for an ancestry check, so the gate would
  still need a fetch or a second remote query.
- **Write to `FETCH_HEAD` and read it back.** Rejected: `FETCH_HEAD` is shared
  mutable state. Any concurrent fetch between the write and the read can
  replace the result.
- **Block when the remote is unreachable.** Rejected: it converts a transient
  network failure into a false block, contrary to the hook's fail-open contract.

## Consequences

- A successful explicit-URL push no longer produces a false unpushed block.
- A URL-valued upstream blocks only when `HEAD` is genuinely ahead.
- Each stop may perform a network fetch. The refresh timeout bounds it, and
  `unpushed_commits_refresh_upstream = false` restores local-ref-only behaviour
  for users who cannot afford the fetch.
- The opt-out compares against a ref that can be stale after an explicit-URL
  push; the user accepts that trade-off by disabling the refresh.
- Tests can inject a command runner and a ref-name factory, so the fetch and
  cleanup sequence is verified without Git or the network.
