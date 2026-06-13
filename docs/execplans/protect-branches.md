# Protect configured branches from unpushed prompts

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`,
and `Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: COMPLETE

## Purpose / big picture

The stop hook currently blocks on unpushed commits whenever the current local
branch is ahead of its configured upstream branch. That is correct for feature
branches, but it can prompt an agent to push directly to protected shared
branches such as `main` or `master`. After this change, users can configure a
set of protected branch names. When the current local branch matches one of
those names, the unpushed-commits gate will not ask the agent to push that
branch.

Success is observable by running the stop hook tests and seeing a protected
local branch skip the unpushed prompt while a normal feature branch still
blocks with the existing push instruction. Users can configure the behaviour
through the existing TOML configuration files loaded by the Cyclopts-backed
entry point.

## Constraints

- Preserve the stop-hook contract: intentional blocks print JSON and exit with
  status `0`; successful checks print nothing.
- Keep configuration wired through `post_turn_quality_stop_hook.config.Config`
  and the existing `--config` Cyclopts option. Do not add a parallel
  configuration surface.
- Keep the existing unpushed prompt for unprotected branches.
- Do not suppress lint, format, type, or test failures. Fix underlying issues.
- Use Makefile targets for repository gates.
- Commit only after the requested gates pass.

## Tolerances (exception triggers)

- Scope: stop and escalate if the implementation requires changes outside
  `post_turn_quality_stop_hook/`, `tests/`, `docs/`, or this plan.
- Interface: stop and escalate if existing public command-line options must
  change incompatibly.
- Dependencies: stop and escalate if a new runtime or development dependency is
  required.
- Iterations: stop and escalate if the same quality gate still fails after
  three fix attempts.
- Ambiguity: stop and present options if protected branch matching must support
  patterns, remote names, or full refs rather than exact local branch names.

## Risks

- Risk: The upstream ref may be `origin/main`, but the local branch name is the
  decision point for whether the hook should ask the agent to push. Severity:
  medium Likelihood: medium Mitigation: Use `current_branch(repo)` before the
  unpushed check and compare the local branch name against configured protected
  branch names.

- Risk: TOML arrays arrive as mutable `list` values, while the frozen dataclass
  should expose an immutable collection. Severity: low Likelihood: high
  Mitigation: Validate every configured branch name is a string, then normalise
  the value to a tuple before constructing `Config`.

- Risk: Documentation formatting may need `make fmt` after Markdown edits.
  Severity: low Likelihood: medium Mitigation: Run the requested gates and use
  formatter output to fix wrapping rather than silencing lint.

## Progress

- [x] (2026-06-13) Read repository guidance and confirmed branch
  `protect-branches`.
- [x] (2026-06-13) Loaded required `leta`, `grepai`, `execplans`, and
  commit-message skills before implementation.
- [x] (2026-06-13) Located configuration loading, unpushed gate behaviour,
  tests, and user documentation.
- [x] (2026-06-13) Drafted the living ExecPlan for configurable branch
  protection.
- [x] (2026-06-13) Loaded `python-router` after it became available, plus the
  routed `python-data-shapes` and `python-testing` skills.
- [x] (2026-06-13) Add configuration tests for default protected branches,
  override merging,
  and invalid protected branch values.
- [x] (2026-06-13) Add behavioural pipeline tests proving protected branches
  skip the push
  prompt and unprotected branches still block.
- [x] (2026-06-13) Implement protected-branch configuration and unpushed-gate
  skip logic.
- [x] (2026-06-13) Update user and developer documentation.
- [x] (2026-06-13) Run `make check-fmt`, `make lint`, `make typecheck`, and
  `make test`
  sequentially through `tee` logs.
- [x] (2026-06-13) Run Markdown gates `make markdownlint` and `make nixie`
  because documentation files changed.
- [x] (2026-06-13) Commit the completed change after gates pass.

## Surprises & discoveries

- Observation: GrepAI is available, but this worktree is not indexed in the
  `Projects` workspace, so project-scoped semantic searches returned no hits.
  Evidence: `grepai search --workspace Projects --project "$(get-project)" ...`
  returned empty result sets. Impact: Use tightly scoped exact search and
  symbol navigation for this task.

- Observation: `python-router` became available after the initial skill list
  did not include it. Evidence: `/home/leynos/.codex/skills/python-router/SKILL.md`
  exists and was loaded after the user noted availability. Impact: Route the
  change through `python-data-shapes` for frozen config normalisation and
  `python-testing` for the unit and behavioural pytest coverage.

## Decision log

- Decision: Treat protected branches as exact local branch names, not full refs
  or glob patterns. Rationale: The user named branch names (`trunk`, `main`,
  `release`, `master`) and the risk is asking an agent to push the current
  local branch. Exact matching keeps behaviour predictable and avoids pattern
  semantics that are not requested. Date/Author: 2026-06-13 / Codex.

- Decision: Store `protected_branches` as a tuple on `Config`.
  Rationale: `Config` is frozen, so normalising TOML arrays to tuples prevents
  accidental mutation after loading while preserving straightforward equality
  assertions in tests. Date/Author: 2026-06-13 / Codex.

## Outcomes & retrospective

Configurable protected branches are implemented. The config boundary now
accepts and validates `protected_branches`, the unpushed gate skips exact local
branch names in that tuple, and docs describe the default protected branch set.
Validation has passed for formatting, linting, type checking, tests, Markdown
linting, and Mermaid diagram checks. The completed implementation has been
committed.

## Context and orientation

`post_turn_quality_stop_hook/config.py` defines the frozen `Config` dataclass
and loads TOML files from XDG, repository-local, and explicit override paths.
The command-line entry point in `post_turn_quality_stop_hook/hook.py` uses
Cyclopts for `--config <path>` and then passes the loaded `Config` through
`StopCheckOptions` to the pipeline.

`post_turn_quality_stop_hook/pipeline.py` contains branch-state gates. The
`unpushed_commits_gate` function currently checks
`has_unpushed_commits(repo, upstream)` and renders
`post_turn_quality_stop_hook/templates/unpushed_required.j2` when the current
`HEAD` is ahead of its upstream ref. `GitFacts` already carries the upstream
ref, and `current_branch(repo)` is available from
`post_turn_quality_stop_hook.git`.

Tests live in `tests/`. Configuration tests belong in `tests/test_config.py`.
Branch-state behavioural tests belong in `tests/test_pipeline.py`, where the
existing tests mock Git functions and assert captured stop-hook JSON.

## Plan of work

First, extend `Config` with `protected_branches`, defaulting to
`("trunk", "main", "release", "master")`. Extend validation so TOML values must
be an array of strings, then normalise loaded values to a tuple before
constructing `Config`. This keeps the existing Cyclopts `--config` mechanism
intact because runtime config already flows through `load_runtime_config`,
`parse_env`, and `StopCheckOptions`.

Second, add a small helper near `unpushed_commits_gate` that determines whether
the current branch is protected. It should call `current_branch(repo)`, return
`False` on Git errors or detached HEAD, and perform exact membership against
`options.config.protected_branches`. `unpushed_commits_gate` should skip before
calling `has_unpushed_commits` when the current branch is protected.

Third, add unit tests for configuration defaults, override behaviour, and type
validation. Add behavioural tests around `unpushed_commits_gate` showing a
protected `main` branch skips the push prompt and an unprotected feature branch
still emits the existing `Please push committed changes` response.

Fourth, document the new `protected_branches` key in `docs/users-guide.md` and
update `docs/developers-guide.md` so future maintainers understand the
branch-state ordering and protected-branch skip.

Finally, run the requested gates sequentially through `tee` logs under `/tmp`,
fix underlying issues if any appear, and commit the result with a file-based
commit message.

## Concrete steps

Work from
`/data/leynos/Projects/post-turn-quality-stop-hook.worktrees/protect-branches`.

Run gates sequentially after implementation:

```bash
make check-fmt 2>&1 | tee /tmp/check-fmt-post-turn-quality-stop-hook-protect-branches.out
make lint 2>&1 | tee /tmp/lint-post-turn-quality-stop-hook-protect-branches.out
make typecheck 2>&1 | tee /tmp/typecheck-post-turn-quality-stop-hook-protect-branches.out
make test 2>&1 | tee /tmp/test-post-turn-quality-stop-hook-protect-branches.out
```

Expected success is each command exiting with status `0`. The test suite should
include new tests covering protected branch configuration and skip behaviour.

## Validation and acceptance

Acceptance requires all of the following:

- `make check-fmt` passes.
- `make lint` passes.
- `make typecheck` passes.
- `make test` passes.
- A TOML config can set `protected_branches = ["develop"]` and produce
  `Config(protected_branches=("develop",))`.
- With default configuration, an unpushed local `main` branch does not emit the
  push-required blocking prompt.
- With default configuration, an unpushed local feature branch still emits the
  push-required blocking prompt.

## Idempotence and recovery

The code edits are ordinary text changes and can be reapplied safely from Git
if interrupted. The gate commands write only to `/tmp` log files and local
build caches managed by the project Makefile. If a gate fails, inspect the
relevant `/tmp/*-post-turn-quality-stop-hook-protect-branches.out` log and fix
the underlying source issue before rerunning that gate.

## Artifacts and notes

Validation artifacts:

```plaintext
/tmp/check-fmt-post-turn-quality-stop-hook-protect-branches.out
/tmp/lint-post-turn-quality-stop-hook-protect-branches.out
/tmp/typecheck-post-turn-quality-stop-hook-protect-branches.out
/tmp/test-post-turn-quality-stop-hook-protect-branches.out
/tmp/markdownlint-post-turn-quality-stop-hook-protect-branches.out
/tmp/nixie-post-turn-quality-stop-hook-protect-branches.out
```

## Interfaces and dependencies

The final interface is the existing TOML configuration surface with one new key:

```toml
protected_branches = ["trunk", "main", "release", "master"]
```

No new dependencies are required. No new command-line options are required.

Revision note: Initial plan created for the configurable protected-branch
implementation. It establishes exact local-branch matching, configuration
normalisation, required tests, documentation, validation, and commit workflow.

Revision note: Implementation added `Config.protected_branches`, normalised
TOML arrays to immutable tuples, skipped the unpushed gate for protected local
branch names, and added focused configuration and branch-state behavioural
tests.

Revision note: Validation completed successfully for the requested Python gates
and the documentation gates required by repository instructions. Remaining work
is limited to committing the validated change.

Revision note: Marked the plan complete for inclusion in the implementation
commit after all required gates passed.

Revision note: Corrected the final plan status and removed stale progress
wording discovered while preparing the pull request.
