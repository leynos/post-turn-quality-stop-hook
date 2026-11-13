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

A relative `XDG_STATE_HOME` is ignored in favour of `$HOME/.local/state`, as
the XDG specification requires. Honouring it would place the log inside the
repository under test, where the uncommitted-changes gate would see it. An
absolute `XDG_STATE_HOME` may still point inside a worktree. In that case the
hook excludes the daily files it actually wrote from the untracked-file list
used by the changed-file query and the uncommitted-changes gate, so the records
it writes cannot block a stop. The exclusion names each written file exactly:
it is not a name pattern, so a pre-existing untracked file in the log directory
is reported as a change exactly as it would be with logging disabled, and the
hook keeps its own file ignored even when a run crosses midnight and writes a
second daily file. A pre-existing untracked file that happens to carry today's
daily name is handled the same way: the hook never appends to it, warns once on
standard error, and writes no records for that run. A daily file that Git
already tracks is treated alike, so committing a daily file cannot turn a clean
worktree dirty and change the decision. Ownership is decided by the first
record in the file: it must carry the hook's producer marker, a known record
type, and the fields every record has. If the log directory's path cannot be
resolved, the hook warns on standard error and keeps the exclusion using a
lexical relative path, so the result is the same as a run with logging
disabled. The path queries ask Git for NUL-separated, unquoted path bytes
(`-z`), so a log directory whose name contains a quote, a backslash, a newline,
a carriage return, or non-ASCII characters still matches its exclusion prefix.

A run with a `start` record but no `finish` record must not be treated as a
pass. Interruption is the usual cause. A catchable signal (`SIGTERM`, `SIGINT`,
or `SIGHUP`) makes a best-effort `finish` record with
`decision = "interrupted"` and the signal name before the hook re-raises the
signal. The handlers are installed before the payload and configuration reads,
so a signal during startup, including one that interrupts a stalled
configuration read, is recorded as well. `SIGKILL` cannot be caught; it leaves
only the `start` record and any progress records written before the kill. A
missing `finish` record can also mean that the log could not be written, or
that a configuration discovered after startup disabled logging and suppressed
every later record. Check standard error for a warning before concluding that
the run was interrupted.

Logging never changes the hook's decision or exit code. If the log cannot be
created or written, the hook warns on standard error and continues; standard
output stays reserved for the JSON decision. `SIGTERM`, `SIGINT`, and `SIGHUP`
are only intercepted when the process was not already ignoring them.

### Retention and opt-out

The hook prunes daily files older than `log_retention_days` (default `14`) once
the retention window is resolved, before the quality checks run. Pruning waits
for the configuration load so a configured window larger than the environment
default never removes files it meant to keep. A value of `0` keeps only the
current day's file. Files without a `hook-YYYY-MM-DD.jsonl` name are left
alone, and so are files the hook cannot prove it created: an expired daily file
is removed only when its first record carries the hook's producer marker, a
known record type, and the fields every record has, and Git confirms the file
is untracked. A Git query that cannot answer counts as "not confirmed", so the
file is kept. That keeps an unrelated untracked file with a matching name, even
one that is otherwise valid JSON, and any committed file, from being destroyed
by retention. If the log directory cannot be scanned or an expired file cannot
be removed, the hook warns on standard error with a fixed message that names no
path, so expired files that accumulate remain visible without exposing a
filesystem location.

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
If a configuration discovered later disables logging, the hook suppresses every
later record; because the file is append-only, a lone `start` record may remain
from that invocation.

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
