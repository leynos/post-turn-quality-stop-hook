# post-turn-quality-stop-hook users' guide

`post-turn-quality-stop-hook` is a Claude Code stop hook. It runs repository
quality gates at the end of a turn and blocks the stop when changed files have
not passed the relevant checks.

The README covers installation and a smoke test. This guide covers runtime
selection rules, configuration, and failure behaviour. See the
[migration note for 0.2.0](migrations/0.2.0.md) for behaviour changes when
upgrading from an earlier release.

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
unpushed_commits_refresh_upstream = true
unpushed_commits_fetch_timeout_seconds = 10.0
max_consecutive_failures = 10
```

Omit `primary_remote` to let the hook choose the primary remote. Unknown keys
block the stop before the hook contacts the network or runs build commands.

Setting `gate_quality_checks = false` disables the quality gate. The hook then
skips build-driver selection, target enumeration, and target execution, and
continues directly to the branch-state gates.

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

The hook also sanitizes the environment it passes to build commands. It removes
`FORCE_COLOR` and `CLICOLOR_FORCE` and sets `NO_COLOR=1`, so a tool cannot
force colour into captured output. Coloured build output is therefore
unavailable through the hook; run the build command directly in a terminal if
you need it.

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

## Repeated failures

A blocked stop sends the agent back to work. When the agent cannot clear the
failure, for example because a toolchain is broken or a gate needs a human, the
hook and the agent would otherwise loop without end. The hook therefore counts
consecutive blocking decisions per working directory.

After `max_consecutive_failures` consecutive blocks (default `10`), the next
stop in that directory is allowed without running any checks. The hook prints a
non-blocking `systemMessage` explaining why, resets the count, and checks again
on the following stop. Any stop that passes also resets the count. Setting
`max_consecutive_failures = 0` disables the mechanism.

Every block counts, including configuration errors and invalid CLI arguments.
When the configuration cannot be loaded, the default limit applies.

The count is keyed on the resolved working directory that the hook evaluates,
not on the repository, so two worktrees, or two subdirectories of one
repository, keep separate counts. It is stored in one small JSON file per
directory under:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/post-turn-quality-stop-hook/failure-streaks/
```

An empty or relative `XDG_STATE_HOME` is ignored, as the XDG Base Directory
specification requires. Deleting the directory resets every count. If the state
directory cannot be read or written, the hook logs a warning and reports the
checks' decision unchanged, so the mechanism fails open rather than hiding a
failure. In particular, the hook only lets a stop through once it has reset the
count; if the reset fails, it runs the checks as usual rather than skipping
every later stop.

## Blocking output

When a quality target fails, the blocking reason includes:

- the configured diff base and resolved merge-base commit,
- changed files, capped at 60 entries,
- detected change categories,
- selected build driver,
- requested, run, and skipped targets,
- failed command output.

The hook removes ANSI escape sequences from captured command output before it
applies the output limit. Colour codes, cursor movement, terminal titles, and
hyperlinks therefore cannot corrupt the report or consume the capture budget.

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
directly onto a shared protected branch. A detached `HEAD` has no branch name,
so it is never treated as protected and uncommitted work still blocks.

The unpushed gate blocks when `HEAD` has commits that are not on the branch's
upstream. The hook reads the upstream from `branch.<name>.remote` and
`branch.<name>.merge`, so it also works when the upstream is a URL from
`git push -u <url>` rather than a named remote. By default the hook fetches the
upstream branch and compares `HEAD` with the fetched head. A local
remote-tracking ref such as `refs/remotes/origin/<branch>` is only a cache and
can stay stale after `git push <url> HEAD:refs/heads/<branch>`, so it is not
authoritative when refreshes are enabled. The refresh fetches into a private
temporary ref, so it does not move the remote-tracking ref and cannot change
the expected value of a later `git push --force-with-lease`. A local upstream
(`branch.<name>.remote = "."`) is compared against that local branch without a
fetch. If the checked-out branch changes while the hook runs, the comparison is
abandoned with a warning, because the upstream would then belong to a different
branch.

If the upstream cannot be resolved or fetched, the gate passes and records a
warning rather than blocking on unverified data. If the branch has no upstream,
the unpushed gate is skipped. If either the current local branch name or the
upstream branch name is listed in `protected_branches`, the unpushed gate is
also skipped so the hook does not ask the agent to push a shared branch
directly. The default protected branches are `trunk`, `main`, `release`, and
`master`. Protected branch skips keep stdout quiet like other successful checks
and are recorded as structured log records for operators who collect hook logs.

Set `unpushed_commits_refresh_upstream = false` to compare against the local
remote-tracking ref instead, for example on an offline machine. That mode can
report a false block after an explicit-URL push and a false skip when the
upstream is a URL, because it trusts the cached ref. The fetch is bounded by
`unpushed_commits_fetch_timeout_seconds`, which defaults to `10.0` and must be
positive, finite, and no greater than `86400` (one day). A timed-out fetch sends
`SIGTERM` so Git can release ref locks, then escalates to `SIGKILL` after a
short bounded grace period. On POSIX that signals the whole process group,
which also stops an SSH transport from being left running; on platforms without
process-group signalling, such as Windows, the direct child is terminated
instead. Credentials in a URL-style upstream are removed from block messages
and warning logs.

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

| Variable                     | Default       | Effect                                                     |
| ---------------------------- | ------------- | ---------------------------------------------------------- |
| `POST_TURN_ALWAYS_FETCH`     | unset         | Fetch the base ref before every quality check when truthy. |
| `POST_TURN_BASE_REF`         | `origin/main` | Base ref used for merge-base and changed-file detection.   |
| `POST_TURN_BUILD_DRIVER`     | `auto`        | Select `auto`, `netsuke`, or `make`.                       |
| `POST_TURN_COMPUSH`          | ignored       | Legacy no-op; use configuration gate toggles.              |
| `POST_TURN_MAKE_BIN`         | `make`        | Make executable name or path.                              |
| `POST_TURN_MAX_OUTPUT_CHARS` | `12000`       | Per-command output capture limit.                          |
| `POST_TURN_NETSUKE_BIN`      | `netsuke`     | Netsuke executable name or path.                           |

_Table 2: Runtime configuration._

Truthy boolean values are `1`, `true`, and `yes`, case-insensitively.

## Troubleshooting

### The hook let a stop through without checking

The hook prints a `systemMessage` naming the number of consecutive blocks when
it short-circuits. See [Repeated failures](#repeated-failures). Raise
`max_consecutive_failures`, or set it to `0`, to change this behaviour.

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
