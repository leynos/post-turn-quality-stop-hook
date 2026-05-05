# post-turn-quality-stop-hook developers' guide

This guide covers the architecture, development workflow, and test strategy for
`post-turn-quality-stop-hook`.

## Architecture

The package exposes one console script:

```toml
[project.scripts]
post-turn-quality-stop-hook = "post_turn_quality_stop_hook.hook:main"
```

Most behaviour lives in `post_turn_quality_stop_hook/hook.py`. The module is
organised as a small pipeline:

1. Parse hook input and environment configuration.
2. Resolve the repository root from the hook working directory.
3. Ensure the configured base ref is resolvable.
4. Compute changed files from the merge-base with `HEAD`.
5. Select a build driver.
6. Map file categories to quality targets.
7. Run available targets and emit a Claude Code blocking response on failure.
8. Optionally run the commit-and-push reminder.

The main state carrier is `HookState`. It keeps user-facing failure output
consistent by collecting the diff base, changed files, categories, selected
driver, target decisions, command results, and any preparation error in one
place.

The build-driver boundary is represented by `BuildDriver` and
`BuildTargetRequest`. Driver-specific behaviour is deliberately narrow:

- target discovery is handled by `get_netsuke_targets` and `get_make_targets`,
- command construction is handled by `build_command`,
- execution is handled by `run_build_targets`.

Keep new driver support inside that boundary. Avoid spreading driver-specific
conditionals across the stop-check pipeline.

## Hook contract

Claude Code stop hooks block by printing a JSON decision. They do not need a
non-zero process exit status to block. This project therefore exits `0` for
both success and intentional block responses.

Preserve these rules:

- print nothing on success,
- print only the blocking JSON payload on quality failure,
- avoid raising uncaught filesystem or Git exceptions from normal hook paths,
- keep non-repository invocations quiet.

## Development environment

Use the repository Make targets. They keep local commands aligned with
Continuous Integration (CI):

```bash
make build
make check-fmt
make lint
make typecheck
make test
```

The full code gate is:

```bash
make all
```

Documentation changes should also pass:

```bash
make fmt
make markdownlint
make nixie
```

`make fmt` runs Ruff import formatting and `mdformat-all`. Run it after
Markdown edits because `mdformat-all` handles wrapping, Markdown lint fixes,
tables, fences, and list numbering.

## Testing strategy

Tests live in `tests/test_hook.py`. They use mocked subprocess calls for most
Git, Make, and Netsuke behaviour so the suite is fast and deterministic.

The tests cover:

- dirty-tree and unpushed-commit detection,
- environment parsing,
- missing working-directory handling,
- build-driver selection,
- Make and Netsuke target discovery,
- category-to-target mapping,
- blocking output and compush integration.

Add tests at the same behavioural level as the change. For example, target
selection changes should exercise `evaluate_changes` or the relevant parser;
environment changes should exercise `parse_env`; hook contract changes should
assert captured standard output.

## Packaging

The project is a Python 3.14 package configured in `pyproject.toml` with
Hatchling as the build backend. The package has no runtime dependencies.
Development dependencies are managed through the `dev` dependency group and
installed by `uv sync --group dev` through `make build`.

Build release artefacts with:

```bash
make build-release
```

## Documentation

Keep user-facing runtime detail in [the users' guide](users-guide.md). Keep the
README focused on installation, first use, and links to deeper material.

Repository documentation follows
[the documentation style guide](documentation-style-guide.md), including
British English, sentence-case headings, fenced-code language identifiers, and
80-column prose wrapping.
