# post-turn-quality-stop-hook

A Claude Code stop hook that runs repository quality checks for changed files
at the end of a turn.

Install the package, then run the hook with the console script:

```bash
post-turn-quality-stop-hook
```

The hook reads the Claude Code stop-hook JSON payload from standard input. It
prints a blocking JSON response when quality gates fail and prints nothing when
the turn may stop.
