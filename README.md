# post-turn-quality-stop-hook

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](
https://deepwiki.com/leynos/post-turn-quality-stop-hook)

*A Claude Code stop hook that runs repository quality checks before a turn can
finish.*

`post-turn-quality-stop-hook` reads Claude Code's stop-hook payload, works out
which files changed, and runs the matching repository quality targets. If a
gate fails, it blocks the stop with a JSON response that tells the agent what
needs attention.

______________________________________________________________________

## Why post-turn-quality-stop-hook?

Claude Code can move quickly. This hook keeps that speed honest:

- **Targeted checks**: run only the quality gates relevant to changed Python,
  TypeScript, Rust, JavaScript, Go, C/C++, Java, Kotlin, Ruby, Swift, or
  Markdown files.
- **Repository-native commands**: prefer `Netsukefile` targets when Netsuke is
  available, then fall back to `Makefile` targets.
- **Clear blocking output**: return changed files, selected targets, and command
  output when the turn should not stop yet.
- **Branch-state gates**: block when work is uncommitted, local commits are
  unpushed, or an open pull request needs rebasing onto its base branch.

______________________________________________________________________

## Quick start

### Installation

Install the package into the Python environment used by Claude Code hooks:

```bash
uv tool install .
```

The hook runs on POSIX systems and on Windows. Cross-process log locking uses
`fcntl` on POSIX and `msvcrt` on Windows. On POSIX the daily file's owner-only
mode is set with `fchmod`. Windows has no descriptor-level mode call, so a new
file inherits the access control of the state directory and the hook cannot
guarantee that other accounts cannot read it; choose a private state directory,
as described in the [users' guide](docs/users-guide.md). Signal-driven
`interrupted` records cover `SIGTERM` and `SIGINT`, plus `SIGHUP` where the
platform defines it.

### Basic usage

Configure Claude Code to run the console script as a stop hook:

```bash
post-turn-quality-stop-hook
```

The command reads the stop-hook JSON payload from standard input. For a quick
manual smoke test, run:

```bash
printf '{}' | post-turn-quality-stop-hook
```

Successful checks print nothing. Failed checks print a blocking JSON response
for Claude Code to display.

______________________________________________________________________

## Features

- Selects changed files relative to `origin/main` by default.
- Supports `POST_TURN_BASE_REF` for custom comparison refs.
- Supports `.post-turn-quality.toml` and `--config <path>` for gate settings.
- Uses Netsuke targets from `Netsukefile` before Make targets from `Makefile`.
- Runs `check-fmt`, `lint`, `typecheck`, `markdownlint`, and `nixie` when
  available.
- Skips unavailable targets instead of assuming every repository has the same
  build surface.
- Captures command output and truncates it with `POST_TURN_MAX_OUTPUT_CHARS`.
- Writes a per-invocation decision log to
  `${XDG_STATE_HOME:-~/.local/state}` by default, recording each allow, block,
  interruption, or error for audit. Logging is best effort: an unwritable state
  directory, a busy daily file, or a failed finish write can leave a run with
  no log, or with a `start` record but no decision, so an absent record is
  evidence to check on standard error rather than proof that a stop passed. The
  log is written only where it cannot change the decision: outside every
  worktree, or at a path Git ignores. A state directory inside a worktree that
  Git does not ignore is refused for the whole run with a warning on standard
  error, and nothing is created, changed, or deleted there, so a stop decides
  the same with logging on or off. A failed log write never changes the
  decision.

______________________________________________________________________

## Learn more

- [Users' Guide](docs/users-guide.md) — configuration and runtime behaviour.
- [Developers' Guide](docs/developers-guide.md) — architecture, testing, and
  development workflow.
- [0.2.0 Migration Guide](docs/migration-0.2.0.md) — decision-log behaviour and
  opt-out.
- [Scripting Standards](docs/scripting-standards.md) — script design guidance
  used by this project.
- [Documentation Style Guide](docs/documentation-style-guide.md) — writing
  conventions for repository documentation.

______________________________________________________________________

## Licence

ISC — see [LICENSE](LICENSE) for details.

______________________________________________________________________

## Contributing

Contributions welcome. Please see [AGENTS.md](AGENTS.md) for repository
guidelines, local gates, and development expectations.
