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
- **Disk-space guard**: skip the gates and allow the stop when a volume the
  checks need is nearly full, while ordinary failures still block.

______________________________________________________________________

## Quick start

### Installation

Install the package into the Python environment used by Claude Code hooks:

```bash
uv tool install .
```

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

## Disk-space guard

A full volume makes every quality target fail for the same reason, and blocking
the stop cannot help the agent free space. Before running targets, the hook
measures the repository's volume and the temporary directory's volume. If
either has fewer than `POST_TURN_MIN_FREE_MB` mebibytes free, it skips the
gates, writes a notice to standard error, and allows the stop. The default is
`100`; set the variable to `0` to disable the pre-check. The temporary
directory is `CLAUDE_CODE_TMPDIR` when set, then `TMPDIR`, then the platform
default.

A gate that fails while its captured output reports `No space left on device`,
`ENOSPC`, or `os error 28` is treated as indeterminate rather than failed, so
the hook writes a notice and allows the stop. Any other failure still produces
the usual blocking response.

______________________________________________________________________

## Learn more

- [Users' Guide](docs/users-guide.md) — configuration and runtime behaviour.
- [Developers' Guide](docs/developers-guide.md) — architecture, testing, and
  development workflow.
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
