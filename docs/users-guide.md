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

## Repository comparison

By default, the hook compares the current repository state with `origin/main`.
For `origin/main`, the hook verifies that the `origin` remote exists and that
`refs/remotes/origin/main` is available.

The hook fetches `origin main` only when the ref is missing. Set
`POST_TURN_ALWAYS_FETCH=1` to fetch every time before checking.

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
3. Otherwise block with a build-driver selection error.

Set `POST_TURN_BUILD_DRIVER` to override automatic selection:

- `auto` keeps the default behaviour.
- `netsuke` requires `Netsukefile` and a Netsuke executable.
- `make` requires `Makefile` and a Make executable.

Custom executable names or paths can be configured with `POST_TURN_NETSUKE_BIN`
and `POST_TURN_MAKE_BIN`.

## Target selection

Changed file extensions determine the requested targets.

| Changed files                                | Requested targets                |
| -------------------------------------------- | -------------------------------- |
| `.py`, `.pyi`, `.ts`, `.tsx`, `.mts`, `.cts` | `check-fmt`, `lint`, `typecheck` |
| `.rs`                                        | `check-fmt`, `lint`              |
| `.md`, `.mdx`, `.markdown`                   | `markdownlint`                   |

_Table 1: File categories and requested quality targets._

Before running targets, the hook enumerates the selected driver's available
targets:

- Make targets are parsed from `make -qp --no-print-directory`.
- Netsuke targets are parsed from `netsuke manifest -`.

Requested targets that are absent from the selected driver are skipped. This
allows small repositories to implement only the quality surface they actually
support.

Code targets and Markdown targets are run as separate command groups. For
example, Python and Markdown changes in a Make repository can run:

```bash
make --no-print-directory check-fmt lint typecheck
make --no-print-directory markdownlint
```

With Netsuke selected, equivalent targets run through:

```bash
netsuke build check-fmt lint typecheck
netsuke build markdownlint
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

## Commit and push reminder

Set `POST_TURN_COMPUSH=1` to add a publication check after quality gates pass.
This mode blocks when either condition is true:

- the working tree has uncommitted, staged, or untracked changes,
- `HEAD` is ahead of the branch's upstream ref.

If the branch has no upstream, dirty work still blocks with a fallback
destination label. A clean branch without an upstream does not block.

Publication-check errors are intentionally quiet. A Git error while checking
dirty state or ahead state exits successfully without output, so the reminder
does not mask quality-gate failures or unrelated repository problems.

## Environment variables

| Variable                     | Default       | Effect                                                   |
| ---------------------------- | ------------- | -------------------------------------------------------- |
| `POST_TURN_ALWAYS_FETCH`     | unset         | Fetch `origin main` before every check when truthy.      |
| `POST_TURN_BASE_REF`         | `origin/main` | Base ref used for merge-base and changed-file detection. |
| `POST_TURN_BUILD_DRIVER`     | `auto`        | Select `auto`, `netsuke`, or `make`.                     |
| `POST_TURN_COMPUSH`          | unset         | Block after successful checks when work is unpublished.  |
| `POST_TURN_MAKE_BIN`         | `make`        | Make executable name or path.                            |
| `POST_TURN_MAX_OUTPUT_CHARS` | `12000`       | Per-command output capture limit.                        |
| `POST_TURN_NETSUKE_BIN`      | `netsuke`     | Netsuke executable name or path.                         |

_Table 2: Runtime configuration._

Truthy boolean values are `1`, `true`, and `yes`, case-insensitively.

## Troubleshooting

### No output appears

No output is the success path. It can also mean the selected directory is not a
Git repository, or no changed files match the supported extensions.

### The hook cannot find a build driver

Add a `Netsukefile` and install `netsuke`, or add a `Makefile` and ensure
`make` is available on `PATH`. For explicit driver selection, ensure the
matching manifest and executable both exist.

### A target is skipped

Skipped targets are requested by file type but absent from the selected build
driver. Add the target to `Netsukefile` or `Makefile` when that gate should run.

### Output is missing the middle of a failure

Increase `POST_TURN_MAX_OUTPUT_CHARS`. The hook truncates long command output
from the middle so the initial context and final error remain visible.
