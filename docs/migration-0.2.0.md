# Migrating to 0.2.0

Version 0.2.0 adds a durable per-invocation decision log. The log is enabled by
default, so an existing installation starts writing files after the upgrade
without any configuration change. The hook's decision, standard output, and
exit code are unchanged.

## What changed

Each invocation appends JSON Lines records to a per-day file:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/post-turn-quality-stop-hook/log/hook-YYYY-MM-DD.jsonl
```

A run writes a `start` record, `progress` records for stages and blocking
commands, and one `finish` record with the decision, timings, repository facts,
quality-check selection, command results, and gate outcomes. The records let an
operator join a run to its Claude Code session through `session_id` and
`transcript_path`, and explain why a stop was allowed or blocked.

The log never contains command output, arbitrary environment variables, or
other hook payload contents. Free-text reasons are collapsed to one line and
truncated to 500 characters. A failed write warns on standard error and never
changes the decision.

## Opting out

Disable logging for a repository in `.post-turn-quality.toml`:

```toml
log_enabled = false
```

Disable it for one environment:

```bash
POST_TURN_LOG=0 post-turn-quality-stop-hook
```

`POST_TURN_LOG` accepts `1`/`true`/`yes`/`on` and `0`/`false`/`no`/`off`, and
overrides `log_enabled`. The `start` record is written before the authoritative
configuration is loaded, so a repository that opts out may still leave the
`start` and input-stage records written by the invocation that discovered the
setting. Every later record is suppressed.

## Retention

The hook prunes daily files older than `log_retention_days` (default `14`) once
the retention window is resolved, before the quality checks run. Set it in
configuration or with `POST_TURN_LOG_RETENTION_DAYS`. A value of `0` keeps the
current day's file and any older file retention may not delete. Files without a
`hook-YYYY-MM-DD.jsonl` name are left alone, as are files that do not carry the
hook's complete record structure: an expired daily file is removed only when
every line carries the hook's producer marker, a known record type, and the
fields every record has, when Git confirms it is untracked, and when it either
lies outside every worktree or is one Git confirms it ignores. Retention scans
only the hook's own log directory, so a log copied elsewhere is never a
candidate. Retention therefore cannot destroy any committed file, or any file
inside a worktree that Git does not ignore, because deleting such a file would
change what the uncommitted-changes gate reports and could change the decision.
An ignored file appears in neither repository query, so removing it keeps
retention bounded for ignored logs without altering what either run reports.
Checking every line rather than the first keeps an unrelated file that merely
starts with a copied hook record. A deliberately forged file is
indistinguishable from one the hook wrote; that boundary is documented in the
users' guide.

## Operational notes

- A relative `XDG_STATE_HOME` is ignored in favour of `$HOME/.local/state`, as
  the XDG specification requires.
- An absolute `XDG_STATE_HOME` may point inside a worktree. The hook then
  excludes the daily files it wrote from the untracked-file list used by the
  changed-file query and the uncommitted-changes gate. The exclusion names each
  file exactly rather than matching a pattern, so every other untracked file in
  the log directory remains visible. A file is excluded once the hook has
  verified it as its own, whether this run created it or it consists solely of
  hook records; an empty file is also treated as the hook's own. Excluding the
  hook's own daily file from an earlier run is deliberate: a logging-disabled
  run never creates one, so treating it as repository content would let logging
  block every later stop that day.
- A daily file the hook does not own is never appended to. That includes a file
  Git already tracks and a pre-existing untracked file with any line that does
  not carry the hook's producer marker. The hook warns once on standard error
  and writes no records for that run, so neither case can make the worktree
  dirty and change the decision.
- A `start` record without a `finish` record usually means the run was
  interrupted. It can also mean the log could not be written. A configuration
  discovered after startup that disables logging writes a terminal finish with
  `decision = "disabled"`, so that case no longer looks like an interruption.
  Check standard error for a warning before treating the run as interrupted.

## Verifying the upgrade

Run the hook once in a repository and inspect the newest file:

```bash
ls "${XDG_STATE_HOME:-$HOME/.local/state}/post-turn-quality-stop-hook/log"
```

The file should contain one `start` record and one `finish` record with the same
`run_id`. If the directory is absent, confirm that `log_enabled` is not
`false` and that `POST_TURN_LOG` is not set to a false token.
