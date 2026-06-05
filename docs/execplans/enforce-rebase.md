# Enforce rebase, publish, and Makefile-driven quality gates in the stop hook

This ExecPlan (execution plan) is a living document. The sections
`Constraints`, `Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work
proceeds.

Status: DRAFT

## Purpose / big picture

The `post-turn-quality-stop-hook` console script runs at the end of a Claude
Code turn and blocks the stop when the repository is not in a healthy state.
Today it inspects file extensions of changed files to decide which Makefile
or Netsuke targets to invoke, and it has an optional reminder
(`POST_TURN_COMPUSH`) that asks the agent to commit or push when the local
work is unpublished.

This change makes the hook a more disciplined release-engineering gate. After
this work, a turn cannot stop when:

- the current branch has diverged from the *local* base ref and the available
  Makefile-defined `check-fmt`, `lint`, or `typecheck` targets fail;
- there are uncommitted changes in the working tree;
- there are committed-but-unpushed commits on a branch that has an upstream;
- the branch has an open GitHub pull request whose base branch on the primary
  remote has moved ahead of the current branch's local merge-base with that
  PR base — in which case the hook instructs the agent to rebase, using a
  specific templated message that names the rebase skill, the merge style,
  and the validation commands.

The absence of a primary remote, of a tracked upstream branch, or of a GitHub
pull request must never on its own block the stop; those signals are simply
unavailable and the corresponding gates are skipped.

Two other shifts ride alongside the new gates. First, the hook stops deciding
which quality targets to run from file extensions: it parses the repository's
`Makefile` and runs whichever of `check-fmt`, `lint`, and `typecheck` are
declared. Netsuke support is retained but Makefile parsing replaces the
extension-driven category logic. Second, all features become individually
configurable through a configuration file (both repo-local and XDG-located),
defaulting to enabled. Configuration loading uses `cyclopts`; user-facing
reject messages are rendered with Jinja; the GitHub API is reached through
`github3.py`; the Makefile is parsed with `make-parser`.

You can observe success in four ways:

1. `printf '{}' | post-turn-quality-stop-hook` on a clean branch that is fully
   pushed and up to date with its PR base prints nothing and exits 0.
2. The same invocation on a branch with uncommitted changes prints a blocking
   JSON payload whose `reason` instructs the agent to commit.
3. The same invocation on a branch whose PR base on `origin` has moved ahead
   prints a blocking JSON payload whose `reason` matches the templated rebase
   instruction verbatim (modulo the configured remote and base branch).
4. Disabling the rebase gate in `.post-turn-quality.toml` makes case (3)
   print nothing.

## Constraints

These are hard invariants that must hold throughout implementation. Violations
must be escalated, not worked around.

- The hook contract with Claude Code does not change. The script reads JSON
  from stdin, returns a process exit code of 0, and may emit at most one
  JSON object on stdout with the shape `{"decision": "block", "reason":
  "..."}`. Hooks must never raise SystemExit with a non-zero code under
  normal conditions because Claude Code treats non-zero exits as
  infrastructure errors rather than policy blocks.
- The console-script entry point remains
  `post-turn-quality-stop-hook = "post_turn_quality_stop_hook.hook:main"`.
- The package must remain importable with `python -c "import
  post_turn_quality_stop_hook"` and the public surface re-exported from
  `post_turn_quality_stop_hook.__init__` must continue to expose `main`.
- The hook must not perform any *destructive* git action. It may run `git
  fetch` against the primary remote and any read-only plumbing commands
  (`rev-parse`, `merge-base`, `diff`, `ls-files`, `rev-list`, `show-ref`,
  `remote`, `for-each-ref`, `config --get`). It must not run `git rebase`,
  `git reset`, `git push`, `git checkout`, `git branch -d`, `git stash`,
  `git clean`, or any porcelain that mutates refs or the working tree.
- The hook must continue to work offline-ish: if the primary remote cannot be
  fetched (network failure, no auth, no remote configured), the hook degrades
  gracefully and skips the gates that need remote knowledge. It must not
  block the stop *because* the network is down. A network error is logged
  into the diagnostic payload but the hook proceeds with whatever local
  information it has.
- The hook must not require a GitHub token. If `GITHUB_TOKEN` (or the
  `gh`-configured token) is not present, the PR lookup is skipped silently
  and the rebase-needed gate is not evaluated. The presence of a PR is
  *additional* information, not a precondition.
- Python target remains 3.14 per `pyproject.toml`. No use of features
  newer than Python 3.14.
- No change to existing behaviour for repositories that contain only a
  `Netsukefile`. The Makefile parser is consulted only when the selected
  driver is `make`.
- All user-facing strings are rendered through Jinja templates so that the
  rebase-request wording is byte-for-byte the wording stipulated by the
  product owner (see `Plan of work` → Milestone 4).

## Tolerances (exception triggers)

If any threshold below is reached, stop and escalate by recording the
situation in `Decision Log` and asking the user for direction.

- Scope: if the diff of net changes exceeds 1500 lines across the package,
  stop and escalate. The implementation should not require it.
- New dependencies beyond those named in the brief (`cyclopts`, `jinja2`,
  `github3.py`, `make-parser`, `betamax`, `pytest-bdd`, `syrupy`,
  `cuprum`, `cmd-mox`) require user confirmation. If a transitive
  requirement forces another runtime dependency, stop and document the
  trade-off.
- Public-API signatures of `hook.main`, `pipeline.run_stop_checks`, and
  `pipeline.prepare_run_stop_checks` may change *names of parameters or
  internal types*, but the script behaviour (stdin/stdout contract, exit
  code, environment-variable defaults) must remain compatible. If a public
  rename of the console-script entry point is required, stop and escalate.
- If `make-parser` cannot parse the repository's existing `Makefile`, stop
  and escalate before falling back to ad-hoc regex parsing of `make -p`
  output (the current implementation). The fallback should be considered
  a deliberate design decision, not a silent workaround.
- If tests fail three times in a row on a single milestone for the same
  underlying reason, stop and escalate rather than continue cycling.
- If `coderabbit review --agent` reports a concern that cannot be resolved
  without violating a constraint, stop and document.
- If a single milestone takes more than four working sessions of effort,
  stop and re-plan.

## Risks

- Risk: `make-parser` may not handle all GNU Make features used by the
  repository's own `Makefile` (conditional `ifneq`, `define ... endef`
  blocks, the `$(call ...)` macro, pattern rules). The repository's own
  `Makefile` already exercises all of these.
  Severity: high. Likelihood: medium.
  Mitigation: write a Hypothesis-light parametric test that asserts the
  parser identifies the three target names we care about
  (`check-fmt`, `lint`, `typecheck`) from the repo's own `Makefile`
  before integrating it into the hook. If the parser cannot, fall back
  to the existing `make -p` probe for those three target names *only*,
  and record the decision.
- Risk: `github3.py` performs synchronous HTTPS requests. A misconfigured
  GitHub Enterprise endpoint or expired token could hang the hook past
  its acceptable latency budget.
  Severity: medium. Likelihood: low.
  Mitigation: wrap all `github3.py` calls in a hard `concurrent.futures`
  timeout of three seconds (configurable). On timeout, treat the PR
  lookup as "no PR" and continue. Add a synthetic test that mocks a
  hanging endpoint with `betamax`-replayed responses combined with a
  forced `time.sleep` in the cassette adapter.
- Risk: detection of the "primary remote" is ambiguous when the repository
  has more than one remote (e.g., a fork has `origin` and `upstream`).
  Severity: medium. Likelihood: medium.
  Mitigation: define a deterministic resolution order (see `Plan of work`
  → Milestone 2). Make the choice overridable via configuration so the
  user can pin a remote name when ambiguity occurs.
- Risk: the templated rebase message contains backticks and double braces.
  Rendering it incorrectly (single-brace escape, missing whitespace) would
  surface as a content-mismatch in snapshot tests.
  Severity: low. Likelihood: low.
  Mitigation: keep the template in a versioned `.j2` file under
  `post_turn_quality_stop_hook/templates/`. Snapshot-test the rendered
  output with `syrupy`.
- Risk: `cuprum` and `cmd-mox` are user-authored projects whose APIs may be
  in flux.
  Severity: low. Likelihood: medium.
  Mitigation: pin both to a known-good commit in `pyproject.toml`'s
  `[tool.uv.sources]` table. Re-evaluate if those crates introduce a
  breaking change during the work.
- Risk: the hook may execute under a partial environment where `git config
  --get merge.conflictStyle` returns nothing, even when the user expects
  `zdiff3`.
  Severity: low. Likelihood: low.
  Mitigation: read the effective merge style with `git config --get
  merge.conflictStyle`, treating absence as "default merge". Pass the
  result to the Jinja template through a boolean
  `three_way_merge_is_configured`.

## Progress

- [ ] Milestone 0: rename branch to `enforce-rebase`. (Completed on
  draft sign-off; record timestamp on actual commit.)
- [ ] Milestone 1: introduce configuration loading via `cyclopts`. Repo-local
  `.post-turn-quality.toml` and XDG
  `$XDG_CONFIG_HOME/post-turn-quality-stop-hook/config.toml` merged with
  precedence repo-local > XDG > defaults. All gates enabled by default.
  Acceptance: a unit test asserts that a repo-local file disabling the
  rebase gate prevents milestone-4 behaviour; an integration test asserts
  that with no config file the defaults still take effect.
- [ ] Milestone 2: replace ad-hoc remote handling with a primary-remote
  resolver and a "git facts" data object that records (primary remote,
  tracked upstream ref, local merge-base with the PR base or upstream).
  Acceptance: `pytest -k git_facts` passes; a behavioural scenario "When
  the repository has multiple remotes Then the configured primary remote
  is selected" passes.
- [ ] Milestone 3: replace the categories-from-extensions code path with a
  Makefile-aware target selector that reads `Makefile` via `make-parser`
  and runs whichever of `check-fmt`, `lint`, `typecheck` are declared.
  Markdown lint continues to be selected when `markdownlint` is a target.
  Acceptance: a parametric test using fixtures of small `Makefile` files
  validates the selected target list.
- [ ] Milestone 4: implement the rebase-needed gate using `github3.py` and
  Jinja-render the prescribed message. Snapshot-test the rendered output
  with `syrupy`. Acceptance: cassette-driven (betamax) tests that
  simulate the PR base being ahead, level, and behind reproduce
  block/pass behaviour.
- [ ] Milestone 5: replace the optional `POST_TURN_COMPUSH` branch with
  configuration-driven uncommitted and unpushed gates that emit Jinja-rendered
  reasons. The gates trigger whenever the repository's branch state warrants
  it, with no environment-variable opt-in. Acceptance: scenario "Given the
  working tree has uncommitted changes When the hook runs Then the stop is
  blocked with a commit reminder" passes.
- [ ] Milestone 6: add the CLI driver wrapper using `cuprum` for argument
  parsing, with `cmd-mox` powering the corresponding behavioural tests.
  Acceptance: invoking `post-turn-quality-stop-hook --config
  /path/to/file.toml` honours the override.
- [ ] Milestone 7: documentation and quality gates. Update `README.md`,
  `docs/users-guide.md`, and `docs/developers-guide.md`. Add `make
  markdownlint` and `make nixie` passes. Run `coderabbit review --agent`
  and clear all concerns.

## Surprises & discoveries

This section is populated during execution.

## Decision log

- Decision: rename the working branch to `enforce-rebase` rather than keep
  the more descriptive `feat/enforce-rebase-plan`.
  Rationale: the user requested it explicitly so that the branch name
  matches the work item; no open PR yet, so a local rename is safe.
  Date/Author: 2026-06-05, plan author.
- Decision: use `betamax` for HTTP cassette recording of `github3.py`
  responses, not `vhs`. Rationale: betamax is the canonical recorder for
  github3.py; the original brief was clarified by the user.
  Date/Author: 2026-06-05, plan author.

## Outcomes & retrospective

Populated at completion of Milestone 7.

## Context and orientation

The working directory is the root of the
`post-turn-quality-stop-hook` package. Source files of interest:

- `post_turn_quality_stop_hook/hook.py` is the CLI entry point. It reads
  stdin JSON, resolves a working directory, parses environment variables
  into a `StopCheckOptions`, and calls `pipeline.run_stop_checks`. After
  this change, the environment-variable parsing remains as a thin
  compatibility layer; the authoritative options come from the
  configuration file.
- `post_turn_quality_stop_hook/pipeline.py` contains
  `prepare_run_stop_checks` (which assembles git facts) and
  `run_stop_checks` (which orchestrates change detection, target
  execution, and the optional commit/push reminder under
  `compush_check`). This file is the centre of the change.
- `post_turn_quality_stop_hook/git.py` is the git plumbing layer.
  `ensure_origin_remote`, `fetch_origin_main`, `ensure_origin_main`, and
  `ensure_base_ref` are origin-specific and become primary-remote-aware
  in Milestone 2. `get_upstream_ref`, `has_uncommitted_changes`, and
  `has_unpushed_commits` already exist and are reused.
- `post_turn_quality_stop_hook/driver.py` enumerates targets via `make
  -p` and a Netsuke manifest scrape. The Makefile path is replaced by
  `make-parser`; the Netsuke path remains unchanged.
- `post_turn_quality_stop_hook/execution.py` defines the command result
  TypedDict and the per-target invocation. After this change the
  `CATS_TO_TARGETS` mapping is replaced by direct introspection of
  Makefile-declared targets in Milestone 3.
- `post_turn_quality_stop_hook/state.py` defines `HookState`,
  `StopCheckOptions`, and `RunStopChecksPreparation`. A new `Config`
  dataclass joins these, holding the merged repo-local + XDG config.
- `post_turn_quality_stop_hook/formatting.py` produces the blocking
  reason. After this change the reason is composed from Jinja templates.

Existing tests under `tests/`:

- `test_cli.py` covers the CLI parsing layer.
- `test_driver.py` covers `make -p` and Netsuke parsing.
- `test_execution.py` covers target invocation and truncation.
- `test_git.py` covers the plumbing helpers.
- `test_pipeline.py` covers the orchestration.

A novice reader should learn the following terms before continuing:

- *Primary remote*: the single remote the hook treats as authoritative for
  fetch and PR lookups. In a single-remote repository it is that remote.
  In a multi-remote repository it is the remote named by the
  `primary_remote` config key, or `origin` if that key is unset, or the
  first remote in `git remote` output if neither `origin` nor a
  configured name is present.
- *Tracked upstream branch*: the ref reported by `git rev-parse
  --abbrev-ref @{u}`. A branch may or may not have one.
- *PR base*: the destination branch of an open GitHub pull request
  associated with the *current local branch's tip*, as reported by the
  GitHub API. The PR base is a branch name on the GitHub host, not a
  local ref.
- *Local base*: the commit returned by `git merge-base <ref> HEAD`, where
  `<ref>` is the local-tracking ref of the PR base
  (e.g., `refs/remotes/origin/main`), or the tracked upstream ref if no
  PR exists, or the configured base ref otherwise.

## Plan of work

The work proceeds as seven milestones; each ends with an explicit go/no-go
validation step. Implementations are tested before they are integrated.

### Milestone 0: branch rename and execplan adoption

Rename the working branch to `enforce-rebase` (already performed during
plan drafting). Commit this ExecPlan as a no-code change. Acceptance: `git
branch --show-current` reports `enforce-rebase` and `git log -1` shows the
ExecPlan addition.

### Milestone 1: configuration loading

Introduce `post_turn_quality_stop_hook/config.py`. Define a `Config`
dataclass that holds the following gate toggles, all defaulting to
`True`:

- `gate_quality_checks` — run `check-fmt`, `lint`, `typecheck` when the
  branch differs from local base.
- `gate_uncommitted_changes` — block the stop if the working tree is dirty.
- `gate_unpushed_commits` — block the stop if HEAD is ahead of upstream.
- `gate_pr_rebase` — block the stop if the PR base is ahead of local base.

Additional fields:

- `primary_remote: str | None` — explicit primary remote name.
- `base_branch_default: str` — Jinja default for the `base_branch`
  variable when the PR base is unknown (defaults to `main`).
- `github_timeout_seconds: float` — timeout for `github3.py` calls
  (defaults to `3.0`).

Add `cyclopts` as a runtime dependency. Loading precedence, highest
first:

1. CLI flag `--config <path>` (added in Milestone 6).
2. Repository-local `.post-turn-quality.toml` at the repo root.
3. XDG `${XDG_CONFIG_HOME:-$HOME/.config}/post-turn-quality-stop-hook/
   config.toml`.
4. In-code defaults.

Unknown keys raise a fatal error before the hook contacts the network or
runs any command; this prevents a typo from silently disabling a gate.

The `cyclopts` integration here is for *config file shape* and
*deserialization*; the CLI parser arrives in Milestone 6.

In `state.py`, change `StopCheckOptions` to hold a `Config` field rather
than the per-flag booleans. Keep `compush` for one release as a
deprecated alias that the legacy environment variable still feeds, but
mark it `# fmt: off` and remove in Milestone 5.

Validation: `pytest tests/test_config.py` covers:

- defaults when no file is present;
- repo-local override;
- XDG override when repo-local absent;
- unknown-key error.

### Milestone 2: primary remote and git facts

Add `post_turn_quality_stop_hook/git_facts.py`. Define:

```python
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class GitFacts:
    primary_remote: str | None
    upstream_ref: str | None
    pr_base_local_ref: str | None
    local_base_commit: str | None
    three_way_merge_is_configured: bool
```

Resolution rules:

- `primary_remote`: configured `primary_remote` if set, else `origin` if
  present, else the lexicographically first remote, else `None`.
- `upstream_ref`: result of `git rev-parse --abbrev-ref @{u}`; `None` on
  failure.
- `pr_base_local_ref`: `<primary_remote>/<pr_base_branch>` when the PR
  lookup (Milestone 4) succeeds.
- `local_base_commit`: `git merge-base` of HEAD with
  `pr_base_local_ref` if set; otherwise with `upstream_ref` if set;
  otherwise with the configured `base_branch_default` resolved against
  the primary remote; otherwise `None`.
- `three_way_merge_is_configured`: `git config --get
  merge.conflictStyle` returns `zdiff3` or `diff3-zealous`.

Refactor `pipeline.prepare_run_stop_checks` to populate a `GitFacts`
object before calling target enumeration. Update `ensure_base_ref` to
accept the primary remote name as a parameter.

Validation: behavioural scenarios in `tests/features/git_facts.feature`
covering the four "When … Then primary remote is …" branches, plus a
unit test asserting that the absence of a primary remote does not raise
or block.

### Milestone 3: Makefile-driven target selection

Replace `CATS_TO_TARGETS` and `detect_categories` with an explicit
`select_targets(makefile_targets: set[str]) -> list[str]`.

Algorithm: if `check-fmt` is among the parsed targets, include it. Same
for `lint`, `typecheck`, and `markdownlint`. Other targets are ignored.

Implement `parse_makefile(path)` in `driver.py` using `make-parser`.
Continue to use the existing Netsuke probe when the driver is
`netsuke`. Remove the `MAKE_TARGET_PROBE`-based `make -p` invocation
*for the make driver* only after Milestone 3's tests pass.

Update `state.HookState` to drop the `categories` field; replace with
`targets_present: list[str]`.

Validation: snapshot tests using `syrupy` over a corpus of small
`Makefile` files (`tests/fixtures/makefiles/*.mk`) assert the parsed
target sets. The repository's own `Makefile` is one of the fixtures.

### Milestone 4: PR-rebase gate

Add `post_turn_quality_stop_hook/github.py`. Define
`lookup_pr(primary_remote: str, branch: str, *, timeout: float) ->
PullRequestSummary | None`. The summary has:

```python
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class PullRequestSummary:
    number: int
    base_branch: str
    base_oid: str
```

Implementation steps:

1. Parse the remote URL to extract `(owner, repo)`. Support both SSH
   (`git@host:owner/repo.git`) and HTTPS (`https://host/owner/repo`)
   forms; strip the `.git` suffix.
2. Initialise a `github3.GitHub` client using the `GITHUB_TOKEN`
   environment variable, falling back to `gh auth token`'s output if
   present. If neither is available, return `None` immediately without
   network contact.
3. Use `concurrent.futures.ThreadPoolExecutor` to enforce the
   configured timeout. On `TimeoutError`, return `None` and record the
   incident on the hook state.
4. Query `repo.pull_requests(state='open', head=f'{owner}:{branch}')`
   and pick the first.

Add the "PR base ahead of local base" check. The remote base OID
(`pr.base.sha` after a fresh fetch) is compared against the local
merge-base commit. If the OIDs differ and `git merge-base --is-ancestor
local_base remote_base` succeeds, the remote is ahead; block the stop
with the rendered template.

Create `post_turn_quality_stop_hook/templates/rebase_required.j2`
populated byte-for-byte from the canonical copy stored alongside this
plan at [`docs/templates/rebase_required.j2`](../templates/rebase_required.j2).
The wording was specified by the product owner and is verified by a
`syrupy` snapshot test against the file's contents, so do not modify
either file in isolation.

Render it via Jinja and emit it as the `reason` of the block payload.

Validation: betamax cassettes under `tests/cassettes/` record three
fixture conversations (PR base ahead, PR base level, no PR). A
syrupy snapshot test asserts byte-for-byte equivalence of the
rendered template with the four combinations of `(zdiff3 configured?,
makefile has typecheck?)`.

### Milestone 5: commit and push gates

Replace `compush_check` with two distinct gates, both rendered with
Jinja templates (`uncommitted_required.j2` and
`unpushed_required.j2`). Both run unconditionally when the
corresponding `Config` toggle is `True`, regardless of the legacy
`POST_TURN_COMPUSH` environment variable, which is removed at this
milestone.

The execution order in `run_stop_checks` is:

1. Quality checks (if `local_base_commit` is set and HEAD differs from
   it).
2. Uncommitted-changes gate.
3. Unpushed-commits gate (requires `upstream_ref` to be present).
4. PR-rebase gate (requires a PR lookup that returned a summary).

The first failing gate emits its block payload and the hook returns
0 with that payload printed. Subsequent gates are not evaluated.

Validation: behavioural scenarios assert each gate fires in isolation
and that the precedence ordering above holds when multiple conditions
are simultaneously true.

### Milestone 6: CLI integration via cuprum

Wrap `hook.main` in a thin `cuprum`-driven CLI that accepts:

- `--config <path>` — explicit configuration file.
- `--print-config` — print the merged configuration and exit 0
  without reading stdin.
- `--dry-run` — perform every gate but always exit 0 with the
  candidate block payload printed to stderr.

These flags are *additions*. With no flags, stdin handling and behaviour
match prior milestones.

`cmd-mox` powers the corresponding behavioural tests. A
`tests/features/cli.feature` file exercises each flag.

Validation: `pytest tests/test_cli.py tests/features/cli.feature`
passes.

### Milestone 7: documentation, snapshot validation, and CodeRabbit

Update `README.md`, `docs/users-guide.md`, and
`docs/developers-guide.md` to describe the new gates, the
configuration file format, the Jinja templates, and the CLI flags.

Run `make check-fmt`, `make lint`, `make typecheck`, `make test`,
`make markdownlint`, and `make nixie` in that order. Each must pass.

Run `coderabbit review --agent` against the branch. Clear every
concern. If the rate limit is hit, sleep using `vsleep` for
`shuf -i 15-30 -n 1` minutes before retrying.

## Concrete steps

Run the following commands from the repository root. Output is captured
to `/tmp/$ACTION-post-turn-quality-stop-hook-enforce-rebase.out` so the
agent can review after each command.

1. Synchronise the workspace and install dev dependencies:

   ```bash
   make build | tee /tmp/build-post-turn-quality-stop-hook-enforce-rebase.out
   ```

2. After every code edit, run the gating commands sequentially (never in
   parallel) so that the shared cargo and uv caches stay coherent:

   ```bash
   make check-fmt | tee /tmp/check-fmt-post-turn-quality-stop-hook-enforce-rebase.out
   make lint      | tee /tmp/lint-post-turn-quality-stop-hook-enforce-rebase.out
   make typecheck | tee /tmp/typecheck-post-turn-quality-stop-hook-enforce-rebase.out
   make test      | tee /tmp/test-post-turn-quality-stop-hook-enforce-rebase.out
   ```

3. Run Markdown gates after any documentation change:

   ```bash
   make markdownlint | tee /tmp/markdownlint-post-turn-quality-stop-hook-enforce-rebase.out
   make nixie        | tee /tmp/nixie-post-turn-quality-stop-hook-enforce-rebase.out
   ```

4. After each milestone, request CodeRabbit's review:

   ```bash
   coderabbit review --agent | tee /tmp/coderabbit-post-turn-quality-stop-hook-enforce-rebase.out
   ```

   If rate-limited, sleep and retry:

   ```bash
   vsleep $(( $(shuf -i 15-30 -n 1) * 60 ))
   coderabbit review --agent | tee /tmp/coderabbit-post-turn-quality-stop-hook-enforce-rebase.out
   ```

5. Commit each milestone separately with a descriptive imperative-mood
   subject line. Do not bundle milestones into a single commit.

## Validation and acceptance

The hook is "done" when all of the following statements are true:

- `make check-fmt`, `make lint`, `make typecheck`, `make test`, `make
  markdownlint`, and `make nixie` each exit 0.
- The behavioural feature files under `tests/features/` describe each
  gate in plain English, and each scenario passes.
- The syrupy snapshot fixtures match for the four `(zdiff3?, typecheck
  target?)` combinations of the rebase template.
- Running `printf '{}' | post-turn-quality-stop-hook` on a clean,
  fully-pushed, rebased branch prints nothing.
- Running the same command on a branch with a single uncommitted file
  prints exactly one JSON object whose `reason` matches the Jinja
  template `uncommitted_required.j2`.
- Running the same command on a branch whose PR base on `origin` is
  ahead prints exactly one JSON object whose `reason` matches
  `rebase_required.j2` rendered with the appropriate variables.
- `coderabbit review --agent` reports no outstanding concerns.

Quality criteria:

- Tests: full `pytest -v -n auto` run passes locally.
- Lint and typecheck: `make lint` and `make typecheck` pass.
- Performance: hook completes within five seconds on the example repo
  even when a network fetch is required.
- Security: no new shell expansion of user-controlled strings; all
  subprocess calls use a list argv form; `noqa: S603` annotations
  retain a justifying comment.

## Idempotence and recovery

Every milestone is committable as a self-contained change. If a
milestone needs to be redone, revert its commit with `git revert
<sha>` (creating a new commit) and re-run from the start of the
milestone. Do not `git reset --hard`.

If `make-parser` integration fails after attempted use, revert
Milestone 3's commit and continue with the existing `make -p`
probe — but record the fallback in `Decision Log` and re-open
the corresponding entry in `Risks`.

If a betamax cassette becomes stale (the GitHub API surface changes),
delete the cassette file and re-record by setting `BETAMAX_RECORD=once`
in the environment and running the affected test. Cassettes must not
contain credentials; the recorder is configured to redact
`Authorization` headers.

## Artifacts and notes

The Jinja template under
`post_turn_quality_stop_hook/templates/rebase_required.j2` must
preserve the two-space indentation that the brief uses around the
sentence beginning "For packaging lock files". The whitespace is
significant for snapshot equality.

Example expected `reason` payload (truncated):

```text
Please rebase this branch onto `origin/main` using the `rebase` skill.

Each time you encounter a conflict, examine the situation carefully with
the tools you have available and formulate a plan before acting.
...
```

## Interfaces and dependencies

Runtime dependencies added in `pyproject.toml`:

- `cyclopts` — configuration file shape and CLI argument parsing.
- `jinja2` — template rendering for all user-facing reason strings.
- `github3.py` — GitHub REST client used to look up the pull request
  associated with the current branch.
- [`make-parser`](https://pypi.org/project/make-parser/) — Makefile
  parsing for target enumeration.

Development dependencies added to the `dev` group:

- `pytest-bdd` — behavioural scenarios.
- `syrupy` — snapshot assertions for rendered templates and parsed
  Makefile targets.
- `betamax` — HTTP cassette recording for `github3.py`.
- `cmd-mox` (`https://github.com/leynos/cmd-mox`) — subprocess mocking
  for CLI tests.

External dependencies pinned via `[tool.uv.sources]`:

- `cuprum = { git = "https://github.com/leynos/cuprum" }` — CLI driver.

New modules and their public surfaces:

- `post_turn_quality_stop_hook.config`:

  ```python
  from dataclasses import dataclass


  @dataclass(slots=True, frozen=True)
  class Config:
      gate_quality_checks: bool
      gate_uncommitted_changes: bool
      gate_unpushed_commits: bool
      gate_pr_rebase: bool
      primary_remote: str | None
      base_branch_default: str
      github_timeout_seconds: float


  def load_config(repo_root: Path, *, override: Path | None = None) -> Config: ...
  ```

- `post_turn_quality_stop_hook.git_facts`:

  ```python
  from dataclasses import dataclass


  @dataclass(slots=True, frozen=True)
  class GitFacts: ...


  def collect_git_facts(repo: Path, config: Config) -> GitFacts: ...
  ```

- `post_turn_quality_stop_hook.github`:

  ```python
  from dataclasses import dataclass


  @dataclass(slots=True, frozen=True)
  class PullRequestSummary: ...


  def lookup_pr(
      remote_url: str, branch: str, *, timeout: float
  ) -> PullRequestSummary | None: ...
  ```

- `post_turn_quality_stop_hook.templates`: a package whose
  `__init__.py` exposes `render(name: str, **vars) -> str`.

Existing surfaces retained:

- `post_turn_quality_stop_hook.hook.main() -> int`.
- `post_turn_quality_stop_hook.pipeline.run_stop_checks(start_cwd: Path,
  base_ref: str, options: StopCheckOptions) -> int`.

## Revision note

Initial draft. The plan introduces seven milestones, defines new modules
under `post_turn_quality_stop_hook/`, and adds five runtime/dev
dependencies. Subsequent revisions should append a one-paragraph note
summarising what changed, why, and how it affects remaining work.
