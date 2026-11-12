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
configuration is loaded, so a repository that opts out may still leave a lone
`start` record from the invocation that discovered the setting.

## Retention

The hook prunes daily files older than `log_retention_days` (default `14`) once
the retention window is resolved, before the quality checks run. Set it in
configuration or with `POST_TURN_LOG_RETENTION_DAYS`. A value of `0` keeps only
the current day's file. Files without a `hook-YYYY-MM-DD.jsonl` name are left
alone, as are files the hook cannot prove it created: an expired daily file is
removed only when its first line is a versioned hook record and Git does not
track it, so retention cannot destroy an unrelated untracked file that shares
the name or any committed file.

## Operational notes

- A relative `XDG_STATE_HOME` is ignored in favour of `$HOME/.local/state`, as
  the XDG specification requires.
- An absolute `XDG_STATE_HOME` may point inside a worktree. The hook then
  excludes its own daily files from the untracked-file list used by the
  changed-file query and the uncommitted-changes gate. Only files named
  `hook-YYYY-MM-DD.jsonl`, with digits in each date position, directly inside
  the log directory are hidden; any other untracked file there, including a
  log-shaped name without an ISO date such as `hook-abcd-ef-gh.jsonl`, and
  tracked files under it, remain visible, because they are repository content
  rather than logging artefacts.
- A daily file that Git already tracks is never appended to. The hook warns
  once on standard error and writes no records for that run, so committing a
  daily file cannot make the worktree dirty and change the decision.
- A `start` record without a `finish` record usually means the run was
  interrupted. It can also mean the log could not be written, or that a
  configuration discovered after startup disabled logging. Check standard error
  for a warning before treating the run as interrupted.

## Verifying the upgrade

Run the hook once in a repository and inspect the newest file:

```bash
ls "${XDG_STATE_HOME:-$HOME/.local/state}/post-turn-quality-stop-hook/log"
```

The file should contain one `start` record and one `finish` record with the same
`run_id`. If the directory is absent, confirm that `log_enabled` is not
`false` and that `POST_TURN_LOG` is not set to a false token.
