# post-turn-quality-stop-hook developers' guide

This guide covers the architecture, development workflow, and test strategy for
`post-turn-quality-stop-hook`.

## Architecture

The package exposes one console script:

```toml
[project.scripts]
post-turn-quality-stop-hook = "post_turn_quality_stop_hook.hook:main"
```

The entry point lives in `post_turn_quality_stop_hook/hook.py`; orchestration
lives in `post_turn_quality_stop_hook/pipeline.py`. The hook is organized as a
small pipeline:

1. Parse CLI arguments, hook input, environment values, and configuration
   files.
2. Resolve the repository root from the hook working directory.
3. Collect best-effort Git facts, including primary remote, upstream ref, and
   merge-conflict style.
4. Ensure the configured base ref is resolvable.
5. Compute changed files from the merge-base with `HEAD`.
6. Select a build driver.
7. In automatic driver mode, skip repository quality targets when neither
   `Netsukefile` nor `Makefile` exists.
8. Map file categories to Make or Netsuke targets that are actually declared.
9. Run available quality targets and emit a Claude Code blocking response on
   failure.
10. Run branch-state gates for uncommitted changes, unpushed commits, protected
    branch commit and push avoidance, and PR rebase requirements.

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

Automatic build-driver selection treats a repository with neither manifest as
"nothing to run" rather than a configuration error. That path must leave stdout
silent, log a bounded structured `quality_gate_skip` record with
`operation=quality_gate_skip`, `build_driver=auto`, and
`manifests_missing=true`, and then continue to branch-state gates. Explicit
`POST_TURN_BUILD_DRIVER=make` or `POST_TURN_BUILD_DRIVER=netsuke` remains
strict: the selected manifest and executable must both exist.

Configuration is represented by `Config` in
`post_turn_quality_stop_hook/config.py`. Loading precedence is explicit config
file, repository-local file, XDG config file, then defaults. CLI parsing uses
Cyclopts and currently accepts only `--config <path>`. TOML arrays such as
`protected_branches` are normalized during config loading so runtime code can
treat the frozen config object as immutable.

User-facing branch-state messages are rendered from Jinja templates under
`post_turn_quality_stop_hook/templates/`. The runtime rebase template is kept
byte-for-byte aligned with `docs/templates/rebase_required.j2`.

Branch-state gates return structured gate decisions. The gate functions decide
whether they pass, skip, or block; `run_branch_state_gates` owns emitting any
blocking JSON payload. Protected-branch skips must keep stdout silent, but
should log bounded structured context such as the gate, outcome, matched
branch, and whether the match was local or upstream.

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

`make lint` runs Ruff followed by a blocking Skylos dead-code scan over
`post_turn_quality_stop_hook`. The CI `lint-test` job runs the same target.
Skylos is provisioned separately at its pinned version and uses only local,
non-interactive dead-code analysis.

Treat each finding as dead code until its runtime caller is verified. Remove
genuine dead code. For a verified false positive that cannot be modelled as an
entry point, record the narrow exception and its caller with:

```bash
make skylos-allow NAME=symbol REASON="verified runtime caller"
```

Documentation changes should also pass:

```bash
make fmt
make markdownlint
make spelling
make nixie
```

`make fmt` runs Ruff import formatting and `mdformat-all`. Run it after
Markdown edits because `mdformat-all` handles wrapping, Markdown lint fixes,
tables, fences, and list numbering.

The spelling gate refreshes the shared en-GB-oxendict dictionary into an
untracked local cache only when the authoritative copy is newer, merges the
repository-specific policy in `typos.local.toml`, and regenerates the tracked
`typos.toml`. Edit the local policy rather than the generated configuration.

## Testing strategy

Tests live in `tests/`. They use mocked subprocess calls for most Git, Make,
GitHub, and Netsuke behaviour so the suite is fast and deterministic. The
Netsuke CLI contract test additionally requires the released `netsuke-build`
v0.1.0-beta1, which CI installs with `cargo-binstall`.

The tests cover:

- dirty-tree and unpushed-commit detection,
- CLI, environment, and configuration parsing,
- missing working-directory handling,
- primary remote and Git-facts collection,
- build-driver selection,
- auto-mode quality-target skip logging when both build manifests are absent,
- Make and Netsuke target discovery, including the released `netsuke generate`
  CLI contract,
- category-to-target mapping,
- blocking output, branch-state gates, and PR-rebase integration,
- protected-branch skip behaviour for uncommitted changes and unpushed commits,
- property tests proving protected local or upstream branches never reach the
  unpushed ahead check,
- Jinja template rendering.

Add tests at the same behavioural level as the change. For example, target
selection changes should exercise `evaluate_changes` or the relevant parser;
environment changes should exercise `parse_env`; CLI changes should exercise
`parse_cli_args` and `main`; hook contract changes should assert captured
standard output.

### Workflow pins and Dependabot

Dependabot owns the upgrade of GitHub Actions and reusable workflows, including
calls into `leynos/shared-actions`. Contract tests that assert a caller's exact
commit SHA create a lockstep dependency: every time Dependabot opens a bump PR,
the test fails until a human edits the pinned constant to match. That defeats
the purpose of automated dependency updates and turns a routine bump into a
manual chore.

Contract tests may still verify the *shape* of a reusable-workflow caller. They
must not verify the specific SHA value.

- Do assert the workflow references the correct reusable workflow path.
- Do assert the ref is pinned to a full 40-character commit SHA, not a
  mutable branch such as `main` or `rolling`.
- Do assert the expected `on:` triggers, least-privilege `permissions:`, and
  the inputs the caller relies on.
- Do not hard-code the current SHA value as an expected string. Match it with
  a pattern instead.
- Do not fail a test purely because Dependabot bumped the pinned SHA.

```python
import re

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def test_uses_pinned_full_sha(caller_step):
    ref = caller_step["uses"].split("@")[-1]
    assert SHA_RE.match(ref), f"expected a 40-hex commit SHA, got {ref!r}"
```

If a workflow's behaviour genuinely depends on a feature only present from a
particular commit onwards, express that as a comment or a changelog note, not
as a test assertion on the SHA string.

## Packaging

The project is a Python 3.14 package configured in `pyproject.toml` with
Hatchling as the build backend. Runtime dependencies are Cyclopts, Jinja2, and
github3.py. Development dependencies are managed through the `dev` dependency
group and installed by `uv sync --group dev` through `make build`.

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
