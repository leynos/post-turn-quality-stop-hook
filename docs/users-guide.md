# post-turn-quality-stop-hook Users' Guide

## Post-Turn Quality Stop Hook

Install this package in the Python environment used by Claude Code hooks. The
package provides this console script:

```bash
post-turn-quality-stop-hook
```

Configure Claude Code to run that command as a stop hook. The command reads the
hook JSON payload from standard input and uses the payload `cwd`, the
`CLAUDE_PROJECT_DIR` environment variable, or the current working directory to
find the repository to check.

By default, the hook compares the current worktree with `origin/main`. It runs
available Makefile targets based on changed file types:

- Python or TypeScript changes run `make check-fmt lint typecheck`.
- Rust changes run `make check-fmt lint`.
- Markdown changes run `make markdownlint`.

The hook skips requested make targets that are not present in the repository's
Makefile. When a target fails, the hook prints a Claude Code blocking response
with the changed files, the selected targets, and the captured command output.

The following environment variables customise the behaviour:

- `POST_TURN_ALWAYS_FETCH=1` always fetches `origin/main` before checking.
- `POST_TURN_BASE_REF=<ref>` changes the comparison ref.
- `POST_TURN_MAX_OUTPUT_CHARS=<number>` changes captured output truncation.
- `POST_TURN_COMPUSH=1` also blocks when local work remains uncommitted or
  committed work has not been pushed to the upstream branch.
