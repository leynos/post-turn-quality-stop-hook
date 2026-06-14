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
those names, the uncommitted-changes gate will not ask the agent to commit
directly onto that branch. When either the current local branch or its tracked
upstream branch matches one of those names, the unpushed-commits gate will not
ask the agent to push that branch.

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
- Keep the existing uncommitted and unpushed prompts for unprotected branches.
- Do not suppress lint, format, type, or test failures. Fix underlying issues.
- Use Makefile targets for repository gates.
- Commit only after the requested gates pass.

## Tolerances (exception triggers)

- Scope: stop and escalate if the implementation requires changes outside
  `post_turn_quality_stop_hook/`, `tests/`, `docs/`, or this plan.
- Interface: stop and escalate if existing public command-line options must
  change incompatibly.
- Dependencies: stop and escalate if a new runtime dependency is required.
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

- Risk: Parsing upstream refs by splitting at the first slash can miss protected
  branches on remotes whose names also contain slashes. Severity: medium
  Likelihood: medium Mitigation: read configured Git remote names and strip the
  longest matching remote prefix before comparing the upstream branch name.

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
  override merging, and invalid protected branch values.
- [x] (2026-06-13) Add behavioural pipeline tests proving protected branches
  skip the push prompt and unprotected branches still block.
- [x] (2026-06-13) Implement protected-branch configuration and unpushed-gate
  skip logic.
- [x] (2026-06-13) Update user and developer documentation.
- [x] (2026-06-13) Run `make check-fmt`, `make lint`, `make typecheck`, and
  `make test` sequentially through `tee` logs.
- [x] (2026-06-13) Run Markdown gates `make markdownlint` and `make nixie`
  because documentation files changed.
- [x] (2026-06-13) Commit the completed change after gates pass.
- [x] (2026-06-13) Record the follow-up requirement that protected local branch
  names must suppress commit prompts and protected tracked branch names must
  suppress push prompts.
- [x] (2026-06-13) Add behavioural tests for protected local commit prompts and
      protected
  tracked upstream push prompts.
- [x] (2026-06-13) Implement protected local and tracked-upstream branch skips.
- [x] (2026-06-13) Validate the follow-up requirement with the full requested
  gates and documentation gates.
- [x] (2026-06-13) Commit the follow-up requirement.
- [x] (2026-06-14) Record the follow-up requirement that slash-containing
  remote names must be stripped as the actual remote prefix before protected
  upstream branch comparison.
- [x] (2026-06-14) Add unit and behavioural coverage for a local branch tracking
  `team/fork/main`.
- [x] (2026-06-14) Implement primary-remote-aware protected upstream parsing.
- [x] (2026-06-14) Validate the slash-containing remote follow-up with the full
  requested gates and documentation gates.
- [x] (2026-06-14) Record review feedback requiring isolated protected-local
  unpushed coverage, gate decision/output separation, property tests,
  structured skip logging, and non-primary slash-remote parsing.
- [x] (2026-06-14) Refactor branch-state gates to return structured decisions
  while `run_branch_state_gates` owns blocking JSON emission.
- [x] (2026-06-14) Add behavioural and Hypothesis coverage proving protected
  local or upstream branches skip the unpushed ahead check.
- [x] (2026-06-14) Add structured branch-state skip/block logs and parse
  upstream branches by the longest configured remote prefix.
- [x] (2026-06-14) Validate review feedback changes with `make check-fmt`,
  `make lint`, `make typecheck`, and `make test`.
- [ ] Commit, push, and refresh the draft pull request.

## Surprises & discoveries

- Observation: GrepAI is available, but this worktree is not indexed in the
  `Projects` workspace, so project-scoped semantic searches returned no hits.
  Evidence: `grepai search --workspace Projects --project "$(get-project)" ...`
  returned empty result sets. Impact: Use tightly scoped exact search and
  symbol navigation for this task.

- Observation: `python-router` became available after the initial skill list
  did not include it. Evidence:
  `/home/leynos/.codex/skills/python-router/SKILL.md` exists and was loaded
  after the user noted availability. Impact: Route the change through
  `python-data-shapes` for frozen config normalisation and `python-testing` for
  the unit and behavioural pytest coverage.

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

- Decision: Apply protected branch names to both local commit prompts and
  tracked upstream push prompts. Rationale: a local branch may track a remote
  branch with a different name, so guarding only the current local branch can
  still instruct the agent to push directly to a protected upstream branch.
  Date/Author: 2026-06-13 / Codex.

- Decision: Strip `GitFacts.primary_remote` from upstream refs before protected
  upstream comparison when it matches. Rationale: dropping only the first path
  segment misparses upstream refs for remotes whose names contain slashes, such
  as `team/fork/main`, and can hide the protected branch name `main`.
  Date/Author: 2026-06-14 / Codex.

- Decision: Use the configured remote list, ordered by longest prefix first,
  when parsing tracked upstream branch names. Rationale: `primary_remote` may be
  `origin` while the actual upstream is `team/fork/main`; the configured
  remote named `team/fork` is the reliable prefix to strip in that case.
  Date/Author: 2026-06-14 / Codex.

- Decision: Keep successful protected-branch skips silent on stdout but log
  bounded structured records with gate, outcome, matched branch, and match
  kind. Rationale: the hook contract requires successful checks to print
  nothing, while review feedback needs observable skip/block outcomes.
  Date/Author: 2026-06-14 / Codex.

- Decision: Add Hypothesis as a development dependency for protected-branch
  gate invariants. Rationale: the protected local/upstream skip behaviour is a
  small input-space invariant, and property tests catch regressions across
  branch sets and slash-containing remote combinations. Date/Author: 2026-06-14
  / Codex.

## Outcomes & retrospective

Configurable protected branches are implemented. The config boundary now
accepts and validates `protected_branches`, the unpushed gate skips exact local
branch names in that tuple, and docs describe the default protected branch set.
Validation has passed for formatting, linting, type checking, tests, Markdown
linting, and Mermaid diagram checks. The completed implementation has been
committed.

The follow-up requirement extends protected branch handling to
uncommitted-change prompts on protected local branches and unpushed-commit
prompts for protected tracked upstream branches. Validation has passed for the
full requested gate set and documentation gates; the follow-up implementation
is ready to commit.

A second follow-up requirement parses protected upstream branch names by
stripping the actual tracked remote prefix, using configured Git remote names
when the upstream remote is not the primary remote. Review feedback then split
branch-state gate evaluation from blocking JSON emission, propagated branch
lookup fallibility into gate decisions, added structured skip/block logging,
and added property tests for protected-branch skip invariants. Validation has
passed for the requested code gates; commit and push are pending.

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
the current branch is protected. It should call `current_branch(repo)`, surface
Git errors in the branch-state decision, and perform exact membership against
`options.config.protected_branches`. `unpushed_commits_gate` should skip before
calling `has_unpushed_commits` when the current branch is protected.

Follow-up: use the same current-branch helper in `uncommitted_changes_gate` so
protected local branches are not prompted for commits. Add a tracked-upstream
helper for refs such as `origin/main`; it should compare the upstream branch
name after the first slash against `protected_branches` so a local `feature`
branch that tracks `origin/main` is not prompted to push to `main`.

Second follow-up: revise the tracked-upstream helper so it strips the longest
matching configured remote prefix when the upstream ref starts with that exact
remote name. This preserves correct behaviour for non-primary remotes with
slash-containing names, such as `team/fork/main`, where the protected upstream
branch is `main`, not `fork/main`.

Review follow-up: split branch-state gate evaluation from stdout emission by
returning a structured decision object from each gate and letting
`run_branch_state_gates` emit blocking JSON. Keep skip outcomes silent on
stdout, but log bounded structured skip/block records. Add Hypothesis
properties for protected local and protected upstream branches to prove those
cases never reach `has_unpushed_commits`.

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
- With default configuration, an uncommitted local `main` branch does not emit
  the commit-required blocking prompt.
- With default configuration, a local `feature` branch tracking `origin/main`
  does not emit the push-required blocking prompt.
- With default configuration, a local `feature` branch tracking
  `team/fork/main` with primary remote `team/fork` does not emit the
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
/tmp/check-fmt-post-turn-quality-stop-hook-protect-branches-followup.out
/tmp/lint-post-turn-quality-stop-hook-protect-branches-followup.out
/tmp/typecheck-post-turn-quality-stop-hook-protect-branches-followup.out
/tmp/test-post-turn-quality-stop-hook-protect-branches-followup.out
/tmp/markdownlint-post-turn-quality-stop-hook-protect-branches-followup.out
/tmp/nixie-post-turn-quality-stop-hook-protect-branches-followup.out
/tmp/check-fmt-post-turn-quality-stop-hook-protect-branches-slash-remote.out
/tmp/lint-post-turn-quality-stop-hook-protect-branches-slash-remote.out
/tmp/typecheck-post-turn-quality-stop-hook-protect-branches-slash-remote.out
/tmp/test-post-turn-quality-stop-hook-protect-branches-slash-remote.out
/tmp/markdownlint-post-turn-quality-stop-hook-protect-branches-slash-remote.out
/tmp/nixie-post-turn-quality-stop-hook-protect-branches-slash-remote.out
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

Revision note: Added the follow-up requirement that protected branch handling
must cover both commit prompts on protected local branches and push prompts to
protected tracked upstream branches.

Revision note: Implemented and validated the follow-up requirement. The
remaining step is to commit and push the update, then refresh the draft pull
request.

Revision note: Marked the validated follow-up requirement complete before the
follow-up commit.

Revision note: Added the slash-containing remote follow-up requirement and the
planned primary-remote-aware parsing change.

Revision note: Implemented and validated primary-remote-aware upstream parsing
for slash-containing remote names. The remaining step is to commit, push, and
refresh the draft pull request.
