# Architectural decision record (ADR) 003: Per-invocation decision log

## Status

Accepted on 2026-11-25. Each hook invocation appends an append-only JSON Lines
record to an XDG state directory, enabled by default, and may write only where
no worktree can report the file.

## Date

2026-11-25.

## Context and problem statement

The hook decides whether a stop is allowed. When it blocks, it prints one JSON
payload that names the reason, but a passing run prints nothing. An operator
who needs to know why a stop was allowed, which gate skipped, or how long a
fetch took has no durable evidence: the process exits, and standard error is
usually discarded by the harness that started it.

The decisions themselves already exist as values. Gates return a decision, the
quality stage returns a selection and its command results, and preparation
returns the facts it collected. What is missing is a place to put them that
survives the process.

The obvious place, standard error, is not free. The hook is a Claude Code stop
hook: standard output carries the JSON decision, and standard error is shared
with whatever a build recipe writes. A log written there is bounded by the
harness's own capture, interleaves with tool output, and cannot be read back
line by line. It also cannot distinguish a run that passed from a run that was
killed, because both are simply a truncated stream.

A durable record has its own hazard: writing a file inside the repository under
test would make a clean worktree dirty, and the uncommitted-changes gate
reports dirty worktrees. Enabling the log could then block a stop that would
otherwise pass, which violates the hook's central rule that observation must
never change the decision.

## Decision drivers

- An operator must be able to explain a decision after the process has exited.
- Enabling the log must not change a decision, an exit code, or standard output.
- The record must survive interruption and be readable line by line.
- Writing must be cheap enough to run on every stop.
- No new runtime dependency may be introduced for it.
- The pipeline must not grow a second responsibility for storage.

## Decision outcome

Write an append-only JSON Lines file per day beneath
`${XDG_STATE_HOME:-$HOME/.local/state}/post-turn-quality-stop-hook/log/`, named
`hook-YYYY-MM-DD.jsonl`. A run writes a `start` record before standard input is
read, `progress` records as stages and blocking commands run, and one terminal
`finish` record carrying the decision, exit code, timings, repository facts,
quality selection, command results, and every gate outcome. A catchable signal
writes an `interrupted` record before it is re-raised.

The log is enabled by default, with `log_enabled` and `log_retention_days` as
its only configuration, and `POST_TURN_LOG` and `POST_TURN_LOG_RETENTION_DAYS`
as environment overrides. A start record without a finish record is not a pass:
it means the run was interrupted, killed, or could not finish writing.

The write location is refused unless Git confirms two facts: it does not track
the daily file, and no worktree could report it as an untracked change. The
second answer is "no" only when the path lies outside every worktree or Git
ignores it there. Any other answer, including one Git cannot establish, refuses
the location for the rest of the run, so a refused log writes and deletes
nothing and cannot alter what a gate sees. A file the hook cannot prove it owns
is never appended to or removed.

The pipeline reaches the log only through `PipelineObserver`. Gates and stage
units report domain events; a recorder-backed observer persists them, and a
no-op observer discards them when logging is disabled. `run_stop_checks` reads
the optional `InvocationLog` once and builds the observer from it, so no gate
function imports, receives, or holds the log. This keeps the recorder
replaceable and lets a test assert the decisions a gate reported.

Full user-facing behaviour, including the refusal rules and the retention
window, is in [the users' guide](../users-guide.md).

## Alternatives considered

- **Log to standard error.** Rejected: standard error is shared with build
  output, is bounded by the harness's capture, and cannot be read back as
  discrete records. A truncated stream cannot tell a passing run from a killed
  one. The issue that tracked stderr logging (#11) is superseded by this
  decision.
- **Gate the log behind a `POST_TURN_DEBUG` variable.** Rejected: a debug
  switch is off exactly when the evidence is needed, which is after an
  unexplained block. It also splits the two code paths, so the debug path is
  the one that rots. Keeping the log on by default and cheap to disable is
  simpler to test and to trust.
- **Write next to the repository, for example under `.git/`.** Rejected: the
  location would then be inside the worktree, and either the uncommitted
  changes gate sees it or a special case must hide it. The refusal rule already
  answers the general question, and the XDG state directory is the conventional
  place for exactly this kind of per-user state.
- **Let the pipeline hold the log and call it directly.** Rejected: every gate
  would then depend on storage, so a logging change could reach a decision path
  and a unit test of a gate would need a log directory. The observer boundary
  keeps storage behind one protocol.
- **Write an unbounded record per run.** Rejected: the reader that verifies a
  file before appending to it has a per-line limit. The writer bounds its own
  records below that limit and drops optional detail rather than emit a line a
  later run would treat as foreign content.

## Consequences

- A blocking decision can be explained after the fact, and a passing run leaves
  the same evidence.
- A run with a `start` record and no `finish` record is visibly incomplete
  rather than indistinguishable from a pass.
- Every run performs file I/O. Retention bounds the number of daily files, and
  each record is one `O_APPEND` write under a short lock.
- A location inside a worktree is usable only when Git ignores it. Users who
  point `XDG_STATE_HOME` inside a repository must ignore the log directory, or
  logging is refused for that run with a warning and no records.
- Records are deliberately narrow: no command output, no arbitrary environment
  values, and no free-text reasons beyond a bounded, one-line field. The log
  cannot be used to reconstruct what a tool printed.
- The observer boundary is now part of the pipeline's shape. A new gate must
  report through it to be visible in the record, and a test that asserts a
  decision can substitute its own observer.
