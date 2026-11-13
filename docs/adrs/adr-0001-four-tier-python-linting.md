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

Skylos runs from the installed `df12-skylos` command (df12-python-lints, pinned
by commit) in a `uv tool` environment with Python 3.14. Skylos parses source
using that interpreter's AST, so this pin prevents phantom findings on syntax
that older runtimes cannot parse. The scan configuration lives in
`[tool.df12_skylos]` and `[tool.skylos]` in `pyproject.toml`.

Investigate every finding. Remove genuine dead code. Model verified implicit
runtime callers with typed Skylos entry-point rules whenever possible. Add a
documented allow-list exception only when an entry-point rule cannot express
the runtime boundary, using both a symbol and a reviewable reason.

## Addendum: whitelist boundary hardening (2026-08-27)

The fourth tier remains a blocking production-only gate. Its documented
exception helper rejects missing and whitespace-only `SYMBOL` and `REASON`
values with exit status 2, including when WSL supplies its hostname through
`NAME`. Update (2026-11-13): the helper now calls `df12-skylos allow`, whose
behaviour and tests live in df12-python-lints; it validates the arguments and
serializes documented allow-list updates with an advisory lock, preventing
concurrent read-modify-write operations from discarding a verified exception.
The local Makefile contract suite and the Makeutil parser it needed were
removed.

## Consequences

- Production dead code blocks local and Continuous Integration (CI) linting.
- Tests do not affect production dead-code reports.
- Contributors need Python 3.14 available to `uv` for Skylos.
