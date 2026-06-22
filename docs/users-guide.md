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
```

Omit `primary_remote` to let the hook choose the primary remote. Unknown keys
block the stop before the hook contacts the network or runs build commands.

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
3. Otherwise skip repository quality targets and continue with branch-state
   gates.

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
- Netsuke targets are parsed from `netsuke manifest -`.

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

### No output appears

No output is the success path. It can also mean the selected directory is not a
Git repository, or no changed files match the supported extensions.

### The hook cannot find a build driver

In automatic driver mode, repositories without `Netsukefile` or `Makefile` skip
repository quality targets and continue with branch-state gates. For explicit
driver selection, ensure the matching manifest and executable both exist.

### A target is skipped

Skipped targets are requested by file type but absent from the selected build
driver. Add the target to `Netsukefile` or `Makefile` when that gate should run.

### Output is missing the middle of a failure

Increase `POST_TURN_MAX_OUTPUT_CHARS`. The hook truncates long command output
from the middle so the initial context and final error remain visible.
