# post-turn-quality-stop-hook users' guide

`post-turn-quality-stop-hook` is a Claude Code stop hook. It runs repository
quality gates at the end of a turn and blocks the stop when changed files have
not passed the relevant checks.

The README covers installation and a smoke test. This guide covers runtime
selection rules, configuration, and failure behaviour.

## Hook contract

The console script is:

```bash
post-turn-quality-stop-hook
```

The command reads Claude Code's stop-hook JSON payload from standard input. The
payload may include a `cwd` field. Repository discovery uses this order:

1. The payload `cwd`, when present and non-empty.
2. The `CLAUDE_PROJECT_DIR` environment variable, when present and non-empty.
3. The process current working directory.

Invalid or missing JSON is treated as an empty payload. If the selected working
directory is not inside a Git repository, the hook exits successfully and
prints nothing. That keeps stop-hook behaviour quiet for unrelated invocations.

The hook follows Claude Code's blocking contract:

- Successful checks print nothing and exit with status `0`.
- Failed checks print JSON with `{"decision": "block", "reason": "..."}` and
  also exit with status `0`.

The command accepts one CLI option:

```bash
post-turn-quality-stop-hook --config /path/to/config.toml
```

An explicit config file has the highest precedence. Invalid CLI arguments are
reported as a blocking JSON payload and still exit with status `0`.

## Configuration files

The hook loads configuration from these locations, highest precedence first:

1. The `--config <path>` file.
2. `.post-turn-quality.toml` in the repository root.
3. `${XDG_CONFIG_HOME:-$HOME/.config}/post-turn-quality-stop-hook/config.toml`.
4. Built-in defaults.

All gates are enabled by default:

```toml
gate_quality_checks = true
gate_uncommitted_changes = true
gate_unpushed_commits = true
gate_pr_rebase = true
base_branch_default = "main"
protected_branches = ["trunk", "main", "release", "master"]
github_timeout_seconds = 3.0
log_enabled = true
log_retention_days = 14
```

Omit `primary_remote` to let the hook choose the primary remote. Unknown keys
block the stop before the hook contacts the network or runs build commands.

## Decision log

The decision log is new in 0.2.0 and is enabled by default. See the
[0.2.0 migration guide](migration-0.2.0.md) for the opt-out and the behaviour
change for existing installations.

Every invocation appends a durable record to a per-day JSON Lines file:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/post-turn-quality-stop-hook/log/hook-YYYY-MM-DD.jsonl
```

Logging is enabled by default. Each run writes three kinds of record:

- a `start` record with `schema_version`, the `producer` marker, `run_id`,
  `timestamp`, `pid`, the hook version, the start directory, and the base ref;
- `progress` records when a stage or build command starts and finishes, so an
  interrupted run still shows what was in progress. One has
  `event = "correlation"` and carries the Claude Code fields `session_id`,
  `transcript_path`, `hook_event_name`, and `stop_hook_active`, which only the
  payload can supply; and
- a `finish` record with the decision (`allow`, `block`, `interrupted`, or
  `error`), exit code, total and per-stage durations, repository facts
  (`repo_root`, `branch`, `head`, `dirty`, `unpushed`), quality-check selection
  (`build_driver`, `build_driver_reason`, and the requested, available, run,
  and skipped targets), each command's exit code and duration, and every gate's
  outcome (`pass`, `skip`, `block`, or `not_reached`) with a short reason.

The `start` record is written before standard input is read, before repository
discovery, and before the authoritative configuration load, so a stall or a
timeout during that work still leaves evidence. The `startup` stage and the
finish record's total duration therefore cover the command-line parsing and log
location work that happens before the log exists. The `input` stage covers the
payload read; the correlation record is written as soon as the payload has been
parsed. Later stages are `prepare`, `quality_checks`, and `branch_state_gates`,
with a separate stage for each branch-state gate (`gate_uncommitted_changes`,
`gate_unpushed_commits`, `gate_pr_rebase`), so a timeout identifies the gate
that was running. Blocking tool invocations are recorded as commands: each
quality build-target group, `netsuke generate` when Netsuke targets are
enumerated, and each `git fetch` that actually runs. A command record carries
the process's own exit code, so a failed step later in the run is never
attributed to a command that succeeded. A base ref that is already present is
verified without fetching and produces no command record.

Gate outcomes distinguish a determined result from an undetermined one. The PR
rebase gate records `pass` only when the base is known not to be ahead. If the
current branch, remote URL, open pull request, base fetch, merge base, or
ancestor comparison cannot be determined, it records `skip` with the reason, so
a failed lookup is never mistaken for a passing check.

`session_id` matches the Claude Code session and joins the run to its
transcript. Every record carries the same `run_id`; the `start` record predates
the payload read, so it carries the `run_id` but not the session fields.

The log never contains command output, arbitrary environment variables, or
other hook payload contents. It records only the documented fields: the base
ref and the selected driver name are settings, while a build executable's
configured path is not logged. Free-text reasons are collapsed to one line and
truncated to 500 characters. Each record is appended with a single `O_APPEND`
write, so concurrent hooks do not interleave partial lines. Daily files are
created with mode `0600`, and an existing file is tightened to `0600` before
each append, so records are not readable by other users.

The `finish` record also carries a `storage` object, with one entry each for
`write` and `prune`. Each entry has a fixed `operation` name (`write` or
`prune`), the number of attempts and failures, accumulated storage and
lock-wait milliseconds, and a sorted list of bounded error categories such as
`BlockingIOError` or `OSError`. These are fixed labels and integers, never
exception text or a path, so an operator can tell a slow or contended log from
a failing one without the log recording anything it must not. The statistics
are captured just before the finish record is appended, so they cover every
earlier record but not the finish record itself.

A relative `XDG_STATE_HOME` is ignored in favour of `$HOME/.local/state`, as
the XDG specification requires. Honouring it would place the log inside the
repository under test, where the uncommitted-changes gate would see it. An
absolute `XDG_STATE_HOME` may still point inside a worktree. In that case the
hook excludes the daily files it wrote from the untracked-file list used by the
changed-file query and the uncommitted-changes gate, so the records it writes
cannot make its own run, or a later one that day, block a stop. The exclusion
names each file exactly: it is not a name pattern, so any other untracked file
in the log directory is reported as a change exactly as it would be with
logging disabled. A daily file is excluded once the hook has verified it as its
own: either this run created it, or every line carries the hook's producer
marker, a known record type, and the fields every record has, and the file ends
with a newline. An empty file counts as the hook's own, because a run killed
between creating the file and writing its first record leaves one behind;
adopting it stops such an interruption from disabling logging for the rest of
the day. Excluding a file an earlier run created is deliberate. A
logging-disabled run would never have created a daily file at all, so treating
the hook's own daily file as repository content would let logging block every
later stop that day; with the exclusion, an enabled run makes the same decision
as a disabled run in a repository that never had the file. The hook keeps both
its files excluded even when a run crosses midnight and creates a second daily
file. A file the hook does not own is never appended to: the hook warns once on
standard error and writes no records for that run, and the file stays visible
to the gates. That covers a pre-existing untracked file with today's daily name
that holds anything other than hook records, and a file that only begins with a
hook record and continues with other data. A daily file that Git already tracks
is treated alike, so committing a daily file cannot turn a clean worktree dirty
and change the decision.

Ownership is a convention, not proof: a file deliberately forged in the log
directory with the daily name and hook-shaped records is indistinguishable from
one the hook wrote, and is therefore treated as the hook's own. The log
directory is resolved to its physical path before the first record is created,
because that is the form Git reports. If the directory cannot be resolved, the
hook warns once on standard error and writes no records, rather than create a
file it could not exclude. Each exclusion records the log directory's physical
path at the moment the hook creates the file, so a symbolic link that cannot be
resolved later cannot leave the run's own log visible to its gates. If the path
still cannot be resolved when a query runs, the hook warns on standard error
and keeps the exclusion using a lexical relative path, so the result is the
same as a run with logging disabled; a directory that may be an outside
symbolic link into the worktree is reported even when its lexical path lies
outside it. A resolved path that leads into the worktree through such a link is
recognized when the hook decides whether Git tracks a file, so a committed
daily file cannot be mistaken for an untracked one. The path queries ask Git
for NUL-separated, unquoted path bytes (`-z`), so a log directory whose name
contains a quote, a backslash, a newline, a carriage return, or non-ASCII
characters still matches its exclusion prefix.

A run with a `start` record but no `finish` record must not be treated as a
pass, but it does not by itself prove that a gate blocked. Interruption is the
usual cause. A catchable signal (`SIGTERM`, `SIGINT`, or `SIGHUP`) makes a
best-effort `finish` record with `decision = "interrupted"` and the signal name
before the hook re-raises the signal. The handlers are installed before the
payload and configuration reads, so a signal during startup, including one that
interrupts a stalled configuration read, is recorded as well. `SIGKILL` cannot
be caught; it leaves only the `start` record and any progress records written
before the kill. A missing `finish` record can also mean that the log could not
be written. A configuration discovered after startup that disables logging
writes a terminal `finish` record with `decision = "disabled"` and then
suppresses every later record, so the opt-out is visible and a missing finish
keeps its interrupted-run meaning. A fatal error, or a record dropped because
another writer held the daily file, can also leave a `start` record without a
`finish` record, so check standard error for a warning before concluding that
the run was interrupted.

Logging never changes the hook's decision or exit code. If the log cannot be
created or written, the hook warns on standard error and continues; standard
output stays reserved for the JSON decision. A warning is dropped rather than
redirected when standard error is missing, closed, or otherwise unwritable, so
a broken diagnostic stream cannot leak text into the decision payload. A
concurrent writer is waited for only briefly, about two seconds: if the daily
file stays locked for longer, the hook drops that record with a warning instead
of stalling. On a host where many hooks write at once, several records can be
dropped this way, including the `finish` record; each drop is reported on
standard error. A record that a signal handler tries to write while the same
thread is already appending is dropped the same way, so catching a signal can
never deadlock the hook. `SIGTERM`, `SIGINT`, and `SIGHUP` are only intercepted
when the process was not already ignoring them.

### Retention and opt-out

The hook prunes daily files older than `log_retention_days` (default `14`) once
the retention window is resolved, before the quality checks run. Pruning waits
for the configuration load so a configured window larger than the environment
default never removes files it meant to keep. A value of `0` keeps the current
day's file, and any older file retention is not allowed to delete, which is the
normal result when the state directory sits inside a repository. Files without a
`hook-YYYY-MM-DD.jsonl` name are left alone, and so are files that do not
carry the hook's complete record structure: an expired daily file is removed
only when every line carries the hook's producer marker, a known record type,
and the fields every record has, the file ends with a newline, Git confirms the
file is untracked, and the file either lies outside every worktree or is one
Git confirms it ignores. Verification reads at most 8 MiB of a file. A larger
daily file cannot be proven to be the hook's own, so it is neither appended to
nor pruned; remove such a file by hand if one appears. Retention only scans the
hook's own log directory, so a copy of a log stored anywhere else is never a
candidate. An ignored file never appears in the changed-file query or the
uncommitted-changes gate, so deleting it leaves both runs reporting the same
visible state and keeps retention bounded for ignored logs when the state
directory is inside a worktree. Any other file inside a worktree is kept even
when this run created it, because deleting an untracked file would change what
the uncommitted-changes gate reports and could turn a block into an allow; a
logging-disabled run would leave the same file visible. A Git query that cannot
answer counts as "not confirmed", so the file is kept. Together these rules
keep an unrelated untracked file with a matching name, even one that is
otherwise valid JSON, any committed file, and any file inside a worktree that
Git does not ignore from being destroyed by retention. If the log directory
cannot be scanned or an expired file cannot be removed, the hook warns on
standard error with a fixed message that names no path, so expired files that
accumulate remain visible without exposing a filesystem location.

Disable logging in configuration:

```toml
log_enabled = false
```

or for a single environment:

```bash
POST_TURN_LOG=0 post-turn-quality-stop-hook
```

`POST_TURN_LOG` accepts `1`/`true`/`yes`/`on` and `0`/`false`/`no`/`off`, and
overrides `log_enabled`. `POST_TURN_LOG_RETENTION_DAYS` overrides
`log_retention_days`; invalid values fall back to the configured or default
retention.

The `start` record is written before the authoritative configuration is loaded.
If a configuration discovered later disables logging, the hook writes a
`finish` record with `decision = "disabled"` and suppresses every later record.
The records written before the opt-out was read, including that finish, remain
in the daily file.

## Repository comparison

By default, the hook compares the current repository state with `origin/main`
for quality checks. For `origin/main`, the hook verifies that the primary
remote exists and that the matching local remote-tracking ref is available.

The primary remote is selected in this order:

1. The configured `primary_remote`, when set and present.
2. `origin`, when present.
3. The lexicographically first configured remote.
4. No primary remote.

The hook fetches the configured base ref only when the ref is missing. Set
`POST_TURN_ALWAYS_FETCH=1` to fetch every time before checking quality gates.
PR-base checks fetch their base branch when they have enough GitHub information
to evaluate the gate.

Changed files are collected from three sources:

- unstaged tracked changes,
- staged tracked changes,
- untracked, non-ignored files.

The comparison point is the merge-base between the configured base ref and
`HEAD`, not the base ref tip. This keeps feature branches with multiple commits
focused on the branch diff.

## Build-driver selection

The hook runs repository-native quality targets through a selected build
driver. Automatic selection uses this order:

1. Use Netsuke when `Netsukefile` exists and `netsuke` is available on `PATH`.
2. Otherwise use Make when `Makefile` exists and `make` is available on `PATH`.
3. When neither `Netsukefile` nor `Makefile` exists, skip repository quality
   targets and continue with branch-state gates.

Automatic mode still reports an error when manifests are present but no usable
build driver remains, such as when the only manifest's matching executable is
missing.

Set `POST_TURN_BUILD_DRIVER` to override automatic selection:

- `auto` keeps the default behaviour.
- `netsuke` requires `Netsukefile` and a Netsuke executable.
- `make` requires `Makefile` and a Make executable.

Custom executable names or paths can be configured with `POST_TURN_NETSUKE_BIN`
and `POST_TURN_MAKE_BIN`.

## Tool environment

The hook inherits the environment supplied by the agent process that runs it.
It does not build a separate environment for `make` or `netsuke`, so those
commands inherit the hook process `PATH`.

In the default Claude Code and Codex CLI configurations, command hooks inherit
the launcher process environment. That means no extra configuration is needed
when `make` and `netsuke` are already visible on `PATH` at the time Claude Code
or Codex CLI starts.

Configuration is needed when tools are available only through an interactive
shell startup file, a login-only profile, `direnv`, `mise`, `nix develop`,
`asdf`, or another shell layer that the agent process did not inherit. Shell
startup files are not a portable way to make tools visible to hooks.

For robust setups, either start the agent from an environment where `PATH`
already contains the required tool directories, or set absolute binary paths:

```bash
export POST_TURN_MAKE_BIN=/usr/bin/make
export POST_TURN_NETSUKE_BIN=/home/example/.local/bin/netsuke
```

Codex users who intentionally restrict subprocess environments should also
ensure their `shell_environment_policy` keeps `PATH` or sets it explicitly.

## Target selection

Changed file extensions determine the requested targets.

| Changed files                                                                                                                                                                                | Requested targets                |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- |
| `.py`, `.pyi`, `.ts`, `.tsx`, `.mts`, `.cts`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.rs`, `.go`, `.c`, `.h`, `.cc`, `.cpp`, `.cxx`, `.hh`, `.hpp`, `.hxx`, `.java`, `.kt`, `.kts`, `.rb`, `.swift` | `check-fmt`, `lint`, `typecheck` |
| `.md`, `.mdx`, `.markdown`                                                                                                                                                                   | `markdownlint`, `nixie`          |

_Table 1: File categories and requested quality targets._

Before running targets, the hook enumerates the selected driver's available
targets:

- Make targets are parsed directly from the repository `Makefile` by matching
  named targets with `^[a-zA-Z0-9_-]+:`.
- Netsuke targets are parsed from `netsuke generate`.

Requested targets that are absent from the selected driver are skipped. This
allows small repositories to implement only the quality surface they actually
support.

Code targets and Markdown targets are run as separate command groups. For
example, Python and Markdown changes in a Make repository can run:

```bash
make --no-print-directory check-fmt lint typecheck
make --no-print-directory markdownlint nixie
```

With Netsuke selected, equivalent targets run through:

```bash
netsuke build check-fmt lint typecheck
netsuke build markdownlint nixie
```

## Blocking output

When a quality target fails, the blocking reason includes:

- the configured diff base and resolved merge-base commit,
- changed files, capped at 60 entries,
- detected change categories,
- selected build driver,
- requested, run, and skipped targets,
- failed command output.

Command output is truncated from the middle when it exceeds the configured
limit, preserving both the beginning and the end. Set
`POST_TURN_MAX_OUTPUT_CHARS` to change the per-command capture limit. The
default is `12000`.

## Branch-state gates

After quality gates pass, the hook evaluates branch-state gates in this order:

1. Uncommitted changes.
2. Unpushed commits.
3. Pull request base branch needs rebasing.

The uncommitted gate blocks when the working tree has uncommitted, staged, or
untracked changes, unless the current local branch name is listed in
`protected_branches`. That prevents the hook from asking an agent to commit
directly onto a shared protected branch.

The unpushed gate blocks when `HEAD` is ahead of the branch's upstream ref. If
the branch has no upstream, the unpushed gate is skipped. If either the current
local branch name or the tracked upstream branch name is listed in
`protected_branches`, the unpushed gate is also skipped so the hook does not
ask the agent to push a shared branch directly. The default protected branches
are `trunk`, `main`, `release`, and `master`. When the tracked remote name
contains a slash, such as `team/fork`, the hook strips the longest matching
configured remote name before comparing the upstream branch name. Protected
branch skips keep stdout quiet like other successful checks and are recorded as
structured log records for operators who collect hook logs.

The PR-rebase gate is best effort. It runs only when the hook can identify a
primary remote, obtain a GitHub token from `GITHUB_TOKEN` or `gh auth token`,
find an open pull request for the current branch, and compare the PR base with
the local merge-base. Missing remote information, missing tokens, network
errors, lookup timeouts, or no open pull request skip this gate rather than
blocking the stop.

Disable individual gates in configuration when a repository needs a looser
policy:

```toml
gate_uncommitted_changes = false
gate_unpushed_commits = false
gate_pr_rebase = false
protected_branches = ["main", "stable"]
```

## Environment variables

| Variable                       | Default       | Effect                                                     |
| ------------------------------ | ------------- | ---------------------------------------------------------- |
| `POST_TURN_ALWAYS_FETCH`       | unset         | Fetch the base ref before every quality check when truthy. |
| `POST_TURN_BASE_REF`           | `origin/main` | Base ref used for merge-base and changed-file detection.   |
| `POST_TURN_BUILD_DRIVER`       | `auto`        | Select `auto`, `netsuke`, or `make`.                       |
| `POST_TURN_COMPUSH`            | ignored       | Legacy no-op; use configuration gate toggles.              |
| `POST_TURN_LOG`                | `1`           | Enable or disable the decision log.                        |
| `POST_TURN_LOG_RETENTION_DAYS` | `14`          | Days of daily decision-log files to keep.                  |
| `POST_TURN_MAKE_BIN`           | `make`        | Make executable name or path.                              |
| `POST_TURN_MAX_OUTPUT_CHARS`   | `12000`       | Per-command output capture limit.                          |
| `POST_TURN_NETSUKE_BIN`        | `netsuke`     | Netsuke executable name or path.                           |

_Table 2: Runtime configuration._

Boolean environment values are `1`, `true`, `yes`, and `on` for true, and `0`,
`false`, `no`, and `off` for false, case-insensitively.

## Troubleshooting

### No output appears

No output is the success path. It can also mean the selected directory is not a
Git repository, or no changed files match the supported extensions.

### The hook cannot find a build driver

In automatic driver mode, the hook skips repository quality targets and
continues with branch-state gates only when neither `Netsukefile` nor
`Makefile` exists. When a supported manifest exists but its matching executable
is unavailable, automatic mode still reports a build-driver error. For explicit
driver selection, ensure the matching manifest and executable both exist.

### A target is skipped

Skipped targets are requested by file type but absent from the selected build
driver. Add the target to `Netsukefile` or `Makefile` when that gate should run.

### Output is missing the middle of a failure

Increase `POST_TURN_MAX_OUTPUT_CHARS`. The hook truncates long command output
from the middle so the initial context and final error remain visible.

### The decision log is empty or missing

Check that `XDG_STATE_HOME` (or `$HOME/.local/state`) is writable and that
logging has not been disabled by `log_enabled = false` or `POST_TURN_LOG=0`. A
write failure warns on standard error and does not change the hook decision. A
`start` record without a matching `finish` record usually means the run was
interrupted; it is not a pass. Check standard error for a write warning, and
check for a later `log_enabled = false`, before concluding that.
