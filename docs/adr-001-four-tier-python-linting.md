# Architectural decision record (ADR) 001: Four-tier Python linting

## Status

Accepted on 2026-08-23. The Python quality architecture has four specialized
tiers, with Skylos as the blocking fourth lint tier for production dead code.

## Date

2026-08-23.

## Context and problem statement

The project already validates Python formatting, static quality, types, and
tests through Make targets. Ruff provides fast syntax, style, and import
feedback, while `ty` checks the type contract. Neither tool determines whether
a reachable-looking production symbol is unused. A dead-code detector must be
strict enough to block regressions without creating false positives from newer
syntax or framework-dispatched runtime callers.

## Decision outcome

The four Python lint tiers are:

1. Ruff formatting and import normalization.
2. Ruff static linting.
3. `ty` static type checking.
4. Skylos strict production dead-code detection.

`make lint` runs the Ruff and Skylos tiers in that order; `make all` runs the
complete architecture. Skylos scans only `post_turn_quality_stop_hook`,
explicitly excludes `tests`, and enables strict gate mode in `pyproject.toml`.

Skylos runs from a pinned, standalone `uv tool` command with Python 3.14.
Skylos parses source using that interpreter's AST, so this pin prevents phantom
findings on syntax that older runtimes cannot parse. Scan configuration is kept
separate from the command-only CLI macro because `skylos whitelist` must
receive its subcommand before any scan-only global options.

Investigate every finding. Remove genuine dead code. Model verified implicit
runtime callers with typed Skylos entry-point rules whenever possible. Add a
documented allow-list exception only when an entry-point rule cannot express
the runtime boundary, using both a symbol and a reviewable reason.

## Consequences

- Production dead code blocks local and Continuous Integration (CI) linting.
- Tests do not affect production dead-code reports.
- Contributors need Python 3.14 available to `uv` for Skylos and the pinned
  Makeutil parser for the Makefile contract tests.
