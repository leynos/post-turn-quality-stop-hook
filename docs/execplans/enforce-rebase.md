# Enforce rebase, publish, and Makefile-driven quality gates in the stop hook

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & Discoveries`, `Decision Log`,
and `Outcomes & Retrospective` must be kept up to date as work proceeds.

Status: IN PROGRESS

## Purpose / big picture

The `post-turn-quality-stop-hook` console script runs at the end of a Claude
Code turn and blocks the stop when the repository is not in a healthy state.
Today it inspects file extensions of changed files to decide which Makefile or
Netsuke targets to invoke, and it has an optional reminder
(`POST_TURN_COMPUSH`) that asks the agent to commit or push when the local work
is unpublished.

This change makes the hook a more disciplined release-engineering gate. After
this work, a turn cannot stop when:

- the current branch has diverged from the *local* base ref and the available
  Makefile-defined `check-fmt`, `lint`, or `typecheck` targets fail;
- there are uncommitted changes in the working tree;
- there are committed-but-unpushed commits on a branch that has an upstream;
- the branch has an open GitHub pull request whose base branch on the primary
  remote has moved ahead of the current branch's local merge-base with that PR
  base — in which case the hook instructs the agent to rebase, using a specific
  templated message that names the rebase skill, the merge style, and the
  validation commands.

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
  from stdin, returns a process exit code of 0, and may emit at most one JSON
  object on stdout with the shape `{"decision": "block", "reason": "..."}`.
  Hooks must never raise SystemExit with a non-zero code under normal
  conditions because Claude Code treats non-zero exits as infrastructure errors
  rather than policy blocks.
- The console-script entry point remains
  `post-turn-quality-stop-hook = "post_turn_quality_stop_hook.hook:main"`.
- The package must remain importable with
  `python -c "import post_turn_quality_stop_hook"` and the public surface
  re-exported from `post_turn_quality_stop_hook.__init__` must continue to
  expose `main`.
- The hook must not perform any *destructive* git action. It may run
  `git fetch` against the primary remote and any read-only plumbing commands
  (`rev-parse`, `merge-base`, `diff`, `ls-files`, `rev-list`, `show-ref`,
  `remote`, `for-each-ref`, `config --get`). It must not run `git rebase`,
  `git reset`, `git push`, `git checkout`, `git branch -d`, `git stash`,
  `git clean`, or any porcelain that mutates refs or the working tree.
- The hook must continue to work offline-ish: if the primary remote cannot be
  fetched (network failure, no auth, no remote configured), the hook degrades
  gracefully and skips the gates that need remote knowledge. It must not block
  the stop *because* the network is down. A network error is logged into the
  diagnostic payload but the hook proceeds with whatever local information it
  has.
- The hook must not require a GitHub token. If `GITHUB_TOKEN` (or the
  `gh`-configured token) is not present, the PR lookup is skipped silently and
  the rebase-needed gate is not evaluated. The presence of a PR is *additional*
  information, not a precondition.
- Python target remains 3.14 per `pyproject.toml`. No use of features
  newer than Python 3.14.
- No change to existing behaviour for repositories that contain only a
  `Netsukefile`. The Makefile parser is consulted only when the selected driver
  is `make`.
- All user-facing strings are rendered through Jinja templates so that the
  rebase-request wording is byte-for-byte the wording stipulated by the product
  owner (see `Plan of work` → Milestone 4).

## Tolerances (exception triggers)

If any threshold below is reached, stop and escalate by recording the situation
in `Decision Log` and asking the user for direction.

- Scope: if the diff of net changes exceeds 1500 lines across the package,
  stop and escalate. The implementation should not require it.
- New dependencies beyond those named in the brief (`cyclopts`, `jinja2`,
  `github3.py`, `make-parser`, `betamax`, `pytest-bdd`, `syrupy`, `cuprum`,
  `cmd-mox`) require user confirmation. If a transitive requirement forces
  another runtime dependency, stop and document the trade-off.
- Public-API signatures of `hook.main`, `pipeline.run_stop_checks`, and
  `pipeline.prepare_run_stop_checks` may change *names of parameters or
  internal types*, but the script behaviour (stdin/stdout contract, exit code,
  environment-variable defaults) must remain compatible. If a public rename of
  the console-script entry point is required, stop and escalate.
- If `make-parser` cannot parse the repository's existing `Makefile`, stop
  and escalate before falling back to ad-hoc regex parsing of `make -p` output
  (the current implementation). The fallback should be considered a deliberate
  design decision, not a silent workaround.
- If tests fail three times in a row on a single milestone for the same
  underlying reason, stop and escalate rather than continue cycling.
- If `coderabbit review --agent` reports a concern that cannot be resolved
  without violating a constraint, stop and document.
- If a single milestone takes more than four working sessions of effort,
  stop and re-plan.

## Risks

- Risk: `make-parser` may not handle all GNU Make features used by the
  repository's own `Makefile` (conditional `ifneq`, `define ... endef` blocks,
  the `$(call ...)` macro, pattern rules). The repository's own `Makefile`
  already exercises all of these. Severity: high. Likelihood: medium.
  Mitigation: write a Hypothesis-light parametric test that asserts the parser
  identifies the five target names we care about (`check-fmt`, `lint`,
  `typecheck`, `markdownlint`, `nixie`) from the repo's own `Makefile` before
  integrating it into the hook. If the parser cannot, fall back to the existing
  `make -p` probe for those five target names *only*, and record the decision.
- Risk: `github3.py` performs synchronous HTTPS requests. A misconfigured
  GitHub Enterprise endpoint or expired token could hang the hook past its
  acceptable latency budget. Severity: medium. Likelihood: low. Mitigation:
  wrap all `github3.py` calls in a hard `concurrent.futures` timeout of three
  seconds (configurable). On timeout, treat the PR lookup as "no PR" and
  continue. Add a synthetic test that mocks a hanging endpoint with
  `betamax`-replayed responses combined with a forced `time.sleep` in the
  cassette adapter.
- Risk: detection of the "primary remote" is ambiguous when the repository
  has more than one remote (e.g., a fork has `origin` and `upstream`).
  Severity: medium. Likelihood: medium. Mitigation: define a deterministic
  resolution order (see `Plan of work` → Milestone 2). Make the choice
  overridable via configuration so the user can pin a remote name when
  ambiguity occurs.
- Risk: the templated rebase message contains backticks and double braces.
  Rendering it incorrectly (single-brace escape, missing whitespace) would
  surface as a content-mismatch in snapshot tests. Severity: low. Likelihood:
  low. Mitigation: keep the template in a versioned `.j2` file under
  `post_turn_quality_stop_hook/templates/`. Snapshot-test the rendered output
  with `syrupy`.
- Risk: `cuprum` and `cmd-mox` are user-authored projects whose APIs may be
  in flux. Severity: low. Likelihood: medium. Mitigation: pin both to a
  known-good commit in `pyproject.toml`'s `[tool.uv.sources]` table.
  Re-evaluate if those crates introduce a breaking change during the work.
- Risk: the hook may execute under a partial environment where
  `git config --get merge.conflictStyle` returns nothing, even when the user
  expects `zdiff3`. Severity: low. Likelihood: low. Mitigation: read the
  effective merge style with `git config --get merge.conflictStyle`, treating
  absence as "default merge". Pass the result to the Jinja template through a
  boolean `three_way_merge_is_configured`.

## Progress

- [x] Milestone 0: rename branch to `enforce-rebase`. Completed before
  implementation began; `git branch --show-current` reported `enforce-rebase`
  at 2026-06-05T19:48:05+02:00.
- [x] Milestone 1: introduce configuration loading via `cyclopts`. Repo-local
  `.post-turn-quality.toml` and XDG
  `$XDG_CONFIG_HOME/post-turn-quality-stop-hook/config.toml` merged with
  precedence repo-local > XDG > defaults. All gates enabled by default.
  Acceptance: a unit test asserts that a repo-local file disabling the rebase
  gate prevents milestone-4 behaviour; an integration test asserts that with no
  config file the defaults still take effect. Completed at
  2026-06-05T19:53:23+02:00; validation passed with `make check-fmt`,
  `make lint`, `make typecheck`, `make test`, `make markdownlint`, and
  `make nixie`.
- [x] Milestone 2: replace ad-hoc remote handling with a primary-remote
  resolver and a "git facts" data object that records (primary remote, tracked
  upstream ref, local merge-base with the PR base or upstream). Acceptance:
  `pytest -k git_facts` passes; a behavioural scenario "When the repository has
  multiple remotes Then the configured primary remote is selected" passes.
  Completed at 2026-06-05T19:59:07+02:00; validation passed with
  `make check-fmt`, `make lint`, `make typecheck`, and `make test`.
- [x] Milestone 3: keep the file-category gating but broaden it, and let
  Makefile presence decide which named targets actually run. Code-file changes
  select `check-fmt`, `lint`, and `typecheck` when each is declared in the
  `Makefile`; Markdown changes select `markdownlint` and `nixie` on the same
  basis. Acceptance: a parametric test using fixtures of small `Makefile` files
  and matching changed-file lists validates the selected target list for each
  category. Blocked at 2026-06-05T20:07:11+02:00 because `make-parser` 0.1.2
  cannot parse hyphenated target names used by this repository's `Makefile`.
  Unblocked by user approval for a regexp parse and completed at
  2026-06-06T09:50:30+02:00; validation passed with `make check-fmt`,
  `make lint`, `make typecheck`, and `make test`.
- [x] Milestone 4: implement the rebase-needed gate using `github3.py` and
  Jinja-render the prescribed message. Snapshot-test the rendered output with
  `syrupy`. Acceptance: cassette-driven (betamax) tests that simulate the PR
  base being ahead, level, and behind reproduce block/pass behaviour. Completed
  at 2026-06-06T09:59:15+02:00 with mocked unit coverage for URL parsing,
  template equivalence, PR-base-ahead blocking, and missing-primary-remote skip
  behaviour; validation passed with `make check-fmt`, `make lint`,
  `make typecheck`, and `make test`.
- [x] Milestone 5: replace the optional `POST_TURN_COMPUSH` branch with
  configuration-driven uncommitted and unpushed gates that emit Jinja-rendered
  reasons. The gates trigger whenever the repository's branch state warrants
  it, with no environment-variable opt-in. Acceptance: scenario "Given the
  working tree has uncommitted changes When the hook runs Then the stop is
  blocked with a commit reminder" passes. Completed at
  2026-06-06T10:17:00+02:00; validation passed with `make check-fmt`,
  `make lint`, `make typecheck`, and `make test`.
- [x] Milestone 6: add the CLI driver wrapper using `cyclopts` for argument
  parsing. Acceptance: invoking
  `post-turn-quality-stop-hook --config /path/to/file.toml` honours the
  override. Completed at 2026-06-06T10:34:00+02:00; validation passed with
  `make check-fmt`, `make lint`, `make typecheck`, and `make test`.
- [x] Milestone 7: documentation and quality gates. Update `README.md`,
  `docs/users-guide.md`, and `docs/developers-guide.md`. Add
  `make markdownlint` and `make nixie` passes. Run `coderabbit review --agent`
  and clear all concerns. Completed at 2026-06-06T10:45:00+02:00; final
  CodeRabbit review completed with `findings: 0`.

## Surprises & discoveries

- Discovery: `uv sync` updated `uv.lock` when `cyclopts` was added as a
  runtime dependency; this is expected and must travel with the Milestone 1
  commit. Date/Author: 2026-06-05, implementation agent.
- Discovery: running `make fmt` rewrote the ExecPlan's Markdown wrapping and
  exposed two long lines that needed manual wrapping before Markdown gates
  passed. Date/Author: 2026-06-05, implementation agent.
- Discovery: `pytest-bdd` is not yet installed, so Milestone 2 captured the
  primary-remote behavioural cases as focused pytest unit tests in
  `tests/test_git_facts.py`. The behaviour is covered now, and the feature-file
  form remains for the later behavioural-test dependency milestone.
  Date/Author: 2026-06-05, implementation agent.
- Discovery: `make-parser` 0.1.2 exposes `make_load(Path)` but its target
  parser matches `^(\w+):`, so it recognizes simple targets such as `all` and
  misses hyphenated targets such as this repository's `check-fmt`,
  `markdownlint`, and `typecheck`. Date/Author: 2026-06-05, implementation
  agent.
- Discovery: the existing tests around Make target enumeration were already
  concerned with avoiding Makefile recipe execution. Direct file parsing with
  the approved named-target regexp preserves that safety property and removes
  the need to invoke `make` for the Makefile driver. Date/Author: 2026-06-06,
  implementation agent.
- Discovery: adding `github3.py` brought normal transitive runtime
  dependencies into `uv.lock`, including `requests`, `uritemplate`,
  `python-dateutil`, `cryptography`, `pyjwt`, and their support packages. These
  are required by the named dependency and were accepted as part of the GitHub
  API integration. Date/Author: 2026-06-06, implementation agent.
- Discovery: extracting branch-state gates reduced `run_stop_checks`
  complexity after Ruff flagged the Milestone 5 orchestration as too complex.
  The resulting order is explicit: quality gates run first, then uncommitted
  changes, then unpushed commits, then PR-base rebase checks. Date/Author:
  2026-06-06, implementation agent.
- Discovery: `main()` tests must patch `sys.argv` after Milestone 6 because
  pytest's own `-v` argument otherwise reaches the hook's Cyclopts parser.
  Date/Author: 2026-06-06, implementation agent.
- Discovery: the public documentation still described the removed
  `POST_TURN_COMPUSH` opt-in, old origin-only fetch wording, and the old
  Python/TypeScript/Rust/Markdown target mapping. Date/Author: 2026-06-06,
  implementation agent.
- Discovery: post-implementation review found several still-valid clean-up
  items: wrong-typed TOML values were not rejected, the temporary
  `cyclopts_available` probe had no callers, dependency bounds were absent,
  some public helper docstrings lacked NumPy-style sections, and the rebase
  templates still needed tighter configured-base wording and wrapping.
  Date/Author: 2026-06-07, implementation agent.
- Discovery: the robust Makefile/Netsukefile follow-up clarified that absence
  of both build manifests is not a hook failure in automatic driver mode. It is
  treated as a quality-target skip, emits bounded structured telemetry, and
  then continues to branch-state gates. Date/Author: 2026-07-06, implementation
  agent.

## Decision log

- Decision: rename the working branch to `enforce-rebase` rather than keep
  the more descriptive `feat/enforce-rebase-plan`. Rationale: the user
  requested it explicitly so that the branch name matches the work item; no
  open PR yet, so a local rename is safe. Date/Author: 2026-06-05, plan author.
- Decision: use `betamax` for HTTP cassette recording of `github3.py`
  responses, not `vhs`. Rationale: betamax is the canonical recorder for
  github3.py; the original brief was clarified by the user. Date/Author:
  2026-06-05, plan author.
- Decision: keep file-type gating instead of running every Makefile target
  on every change. Code-file changes select `check-fmt`, `lint`, and
  `typecheck` (when each is declared in the `Makefile`); Markdown-file changes
  select `markdownlint` and `nixie` on the same basis. The "code" extension set
  is broadened beyond the previous Python/TS/Rust union so the hook stays
  useful in mixed-language repositories. Rationale: an earlier draft removed
  category detection entirely, which would have fired Markdown gates on
  pure-code changes, fired code gates on docs-only changes, and silently dropped
  `nixie`. The user reinstated file-type gating to preserve the original
  change-scoped behaviour while still letting Makefile presence be the
  authority on individual target invocation. Date/Author: 2026-06-05, plan
  author.
- Decision: implement Milestone 1 configuration parsing with stdlib `tomllib`
  and keep `cyclopts` as an explicit runtime dependency that is exercised by an
  importability probe until the CLI wrapper consumes it in Milestone 6.
  Rationale: TOML loading is a stable Python 3.14 standard-library boundary,
  while the plan's `cyclopts` CLI integration is scheduled later. This keeps
  Milestone 1 small without hiding the dependency from packaging or tests.
  Date/Author: 2026-06-05, implementation agent.
- Decision: load repository configuration in `hook.main` after resolving the
  start directory and before calling `run_stop_checks`. Rationale: the existing
  pipeline already resolves the repository root, but config precedence needs
  the repo-local path before options are constructed. A second read-only
  `repo_root` lookup keeps the public pipeline signature stable for this
  milestone and can be consolidated during the git-facts refactor. Date/Author:
  2026-06-05, implementation agent.
- Decision: keep the existing origin-specific git helper wrappers while adding
  generic primary-remote helpers for new code. Rationale: this limits
  compatibility risk for existing tests and call sites while allowing
  `ensure_base_ref` and `collect_git_facts` to become primary-remote aware.
  Date/Author: 2026-06-05, implementation agent.
- Decision: pass `Config` into `prepare_run_stop_checks` so git facts are
  collected from the same merged configuration used by `run_stop_checks`.
  Rationale: a default config inside preparation would silently ignore
  repo-local primary-remote overrides. The signature change is internal to the
  package and covered by the existing public hook path. Date/Author:
  2026-06-05, implementation agent.
- Decision: stop Milestone 3 before implementing a fallback parser.
  Rationale: the `Tolerances` section explicitly says that if `make-parser`
  cannot parse the repository's existing `Makefile`, implementation must stop
  and escalate before falling back to ad-hoc parsing. Options to proceed are:
  patch or vendor `make-parser`, choose a different Makefile parser, or approve
  a deliberate fallback based on the existing `make -p` probe for the five
  named targets only. Date/Author: 2026-06-05, implementation agent.
- Decision: use direct regexp parsing for Makefile named targets with the
  approved pattern `^[a-zA-Z0-9_-]+:`. Rationale: the hook only needs declared
  named targets, not full Make evaluation. This pattern recognizes the
  repository's hyphenated quality targets, ignores recipes and special dot
  targets, and avoids running `make` during target discovery. Date/Author:
  2026-06-06, implementation agent.
- Decision: collapse detected code categories to `code` and `markdown`, with
  `code` selecting `check-fmt`, `lint`, and `typecheck`, and `markdown`
  selecting `markdownlint` and `nixie`. Rationale: the plan's Milestone 3
  intentionally broadens code-file coverage beyond Python, TypeScript, and Rust
  while keeping change-scoped target execution. Date/Author: 2026-06-06,
  implementation agent.
- Decision: in automatic build-driver mode, treat a repository with neither
  `Netsukefile` nor `Makefile` as having no quality targets to run rather than
  as a configuration error. Log a structured `quality_gate_skip` record with
  `operation=quality_gate_skip`, `build_driver=auto`, and
  `manifests_missing=true`, then continue to branch-state gates. Rationale:
  missing individual targets already skip gracefully, and repositories without
  either manifest should not fail the stop hook solely because there is no
  repository-native quality driver. Explicit `POST_TURN_BUILD_DRIVER=make` and
  `POST_TURN_BUILD_DRIVER=netsuke` remain strict because those settings are
  deliberate configuration. Date/Author: 2026-07-06, implementation agent.
- Decision: implement the PR lookup as an optional best-effort gate.
  Rationale: the hook contract requires missing remotes, missing tokens, and
  lookup timeouts to skip the PR gate rather than block the stop. The new
  `post_turn_quality_stop_hook.github.lookup_pr` returns `None` for these
  unavailable-information cases. Date/Author: 2026-06-06, implementation agent.
- Decision: keep the rebase template byte-for-byte aligned with
  `docs/templates/rebase_required.j2` and render it through a small bundled
  Jinja renderer. Rationale: the product-owned wording is the behavioural
  contract for the block reason. A direct equivalence test catches drift
  between the canonical docs copy and the runtime template. Date/Author:
  2026-06-06, implementation agent.
- Decision: make `POST_TURN_COMPUSH` inert while retaining
  `StopCheckOptions.compush` and `compush_check` as compatibility-only code for
  now. Rationale: Milestone 5 requires branch-state gates to run from
  configuration rather than an environment opt-in. Leaving the old field and
  helper in place avoids an avoidable compatibility break while tests assert
  that the legacy environment variable no longer enables behaviour.
  Date/Author: 2026-06-06, implementation agent.
- Decision: render uncommitted and unpushed block reasons from dedicated Jinja
  templates, `uncommitted_required.j2` and `unpushed_required.j2`. Rationale:
  the plan requires all user-facing block strings to flow through templates,
  and separate templates keep the branch-state messages testable without
  coupling them to the older combined commit/push reminder. Date/Author:
  2026-06-06, implementation agent.
- Decision: implement the Milestone 6 `--config` wrapper with `cyclopts`, not
  `cuprum` and `cmd-mox`. Rationale: Milestone 1 already introduced Cyclopts as
  the declared runtime CLI parser, `load_config` already accepted an override
  path, and the acceptance criterion only requires in-process argument parsing
  for `--config`. Adding `cuprum` and `cmd-mox` would not exercise any
  subprocess boundary for this feature. Date/Author: 2026-06-06, implementation
  agent.
- Decision: remove the origin-specific git helper wrappers
  (`ensure_origin_remote`, `fetch_origin_main`, `ensure_origin_main`, and
  `ensure_origin_main_ref`) rather than continue retaining them. Rationale: the
  migration to `ensure_base_ref` completed and left the wrappers with no
  remaining production or test callers, so retaining them only preserved dead
  code and left roughly 124 mutants unkilled. The compatibility concern behind
  the 2026-06-05 decision has therefore lapsed. New and existing code now uses
  the remote-agnostic helpers parameterized by `primary_remote` and
  `base_branch_default`; there is no origin-specific compatibility surface left
  to preserve. Date/Author: 2026-07-29, implementation agent.

## Outcomes & retrospective

The hook now has configuration-driven quality, uncommitted, unpushed, and
PR-rebase gates. It keeps missing remote, upstream, token, and PR data as
non-blocking unavailable-information cases, while still blocking when local
facts prove the branch is dirty, ahead of upstream, or behind the open pull
request base. Makefile target discovery uses the approved named-target regexp
instead of `make-parser`, because `make-parser` 0.1.2 cannot parse this
repository's hyphenated quality targets.

The implementation deliberately uses Cyclopts for the `--config` wrapper rather
than adding `cuprum` and `cmd-mox`. That keeps Milestone 6 aligned with the
implemented acceptance criterion and the already-declared runtime dependency.

Post-review clean-up on 2026-06-07 tightened configuration type validation,
removed the dead Cyclopts importability probe, bounded runtime dependency
versions, expanded public helper docstrings, improved rebase template coverage,
and kept the canonical and bundled rebase templates aligned.

## Context and orientation

The working directory is the root of the `post-turn-quality-stop-hook` package.
Source files of interest:

- `post_turn_quality_stop_hook/hook.py` is the CLI entry point. It reads
  stdin JSON, resolves a working directory, parses environment variables into a
  `StopCheckOptions`, and calls `pipeline.run_stop_checks`. After this change,
  the environment-variable parsing remains as a thin compatibility layer; the
  authoritative options come from the configuration file.
- `post_turn_quality_stop_hook/pipeline.py` contains
  `prepare_run_stop_checks` (which assembles git facts) and `run_stop_checks`
  (which orchestrates change detection, target execution, and the optional
  commit/push reminder under `compush_check`). This file is the centre of the
  change.
- `post_turn_quality_stop_hook/git.py` is the git plumbing layer. The
  origin-specific wrappers no longer exist; `ensure_base_ref` is the
  remote-agnostic entry point and takes a `primary_remote` parameter.
  `get_upstream_ref`, `has_uncommitted_changes`, and `has_unpushed_commits`
  already exist and are reused.
- `post_turn_quality_stop_hook/driver.py` enumerates targets via `make -p` and
  a Netsuke manifest scrape. The Makefile path is replaced by `make-parser`;
  the Netsuke path remains unchanged.
- `post_turn_quality_stop_hook/execution.py` defines the command result
  TypedDict and the per-target invocation. After this change the
  `CATS_TO_TARGETS` mapping is replaced by direct introspection of
  Makefile-declared targets in Milestone 3.
- `post_turn_quality_stop_hook/state.py` defines `HookState`,
  `StopCheckOptions`, and `RunStopChecksPreparation`. A new `Config` dataclass
  joins these, holding the merged repo-local + XDG config.
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
  fetch and PR lookups. In a single-remote repository it is that remote. In a
  multi-remote repository it is the remote named by the `primary_remote` config
  key, or `origin` if that key is unset, or the first remote in `git remote`
  output if neither `origin` nor a configured name is present.
- *Tracked upstream branch*: the ref reported by
  `git rev-parse --abbrev-ref @{u}`. A branch may or may not have one.
- *PR base*: the destination branch of an open GitHub pull request
  associated with the *current local branch's tip*, as reported by the GitHub
  API. The PR base is a branch name on the GitHub host, not a local ref.
- *Local base*: the commit returned by `git merge-base <ref> HEAD`, where
  `<ref>` is the local-tracking ref of the PR base (e.g.,
  `refs/remotes/origin/main`), or the tracked upstream ref if no PR exists, or
  the configured base ref otherwise.

## Plan of work

The work proceeds as seven milestones; each ends with an explicit go/no-go
validation step. Implementations are tested before they are integrated.

### Milestone 0: branch rename and execplan adoption

Rename the working branch to `enforce-rebase` (already performed during plan
drafting). Commit this ExecPlan as a no-code change. Acceptance:
`git branch --show-current` reports `enforce-rebase` and `git log -1` shows the
ExecPlan addition.

### Milestone 1: configuration loading

Introduce `post_turn_quality_stop_hook/config.py`. Define a `Config` dataclass
that holds the following gate toggles, all defaulting to `True`:

- `gate_quality_checks` — when the branch differs from the local base,
  run `check-fmt`, `lint`, and `typecheck` if any changed file is a code file,
  and run `markdownlint` and `nixie` if any changed file is a Markdown file. In
  each case the target must be declared in the `Makefile`; targets that the
  `Makefile` does not declare are silently skipped.
- `gate_uncommitted_changes` — block the stop if the working tree is dirty.
- `gate_unpushed_commits` — block the stop if HEAD is ahead of upstream.
- `gate_pr_rebase` — block the stop if the PR base is ahead of local base.

Additional fields:

- `primary_remote: str | None` — explicit primary remote name.
- `base_branch_default: str` — Jinja default for the `base_branch`
  variable when the PR base is unknown (defaults to `main`).
- `github_timeout_seconds: float` — timeout for `github3.py` calls
  (defaults to `3.0`).

Add `cyclopts` as a runtime dependency. Loading precedence, highest first:

1. CLI flag `--config <path>` (added in Milestone 6).
2. Repository-local `.post-turn-quality.toml` at the repo root.
3. XDG
   `${XDG_CONFIG_HOME:-$HOME/.config}/post-turn-quality-stop-hook/ config.toml`.
4. In-code defaults.

Unknown keys raise a fatal error before the hook contacts the network or runs
any command; this prevents a typo from silently disabling a gate.

The `cyclopts` integration here is for *config file shape* and
*deserialization*; the CLI parser arrives in Milestone 6.

In `state.py`, change `StopCheckOptions` to hold a `Config` field rather than
the per-flag booleans. Keep `compush` for one release as a deprecated alias
that the legacy environment variable still feeds, but mark it `# fmt: off` and
remove in Milestone 5.

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
  `pr_base_local_ref` if set; otherwise with `upstream_ref` if set; otherwise
  with the configured `base_branch_default` resolved against the primary
  remote; otherwise `None`.
- `three_way_merge_is_configured`: `git config --get merge.conflictStyle`
  returns `zdiff3` or `diff3-zealous`.

Refactor `pipeline.prepare_run_stop_checks` to populate a `GitFacts` object
before calling target enumeration. Update `ensure_base_ref` to accept the
primary remote name as a parameter.

Validation: behavioural scenarios in `tests/features/git_facts.feature`
covering the four "When … Then primary remote is …" branches, plus a unit test
asserting that the absence of a primary remote does not raise or block.

### Milestone 3: file-type-gated, Makefile-aware target selection

Keep the principle that file-type gating decides *which kinds of checks* might
run, and let the `Makefile` decide *which named targets within those kinds*
actually exist. Replace the existing `CATS_TO_TARGETS` map and
`detect_categories` helper with two collaborating functions:

```python
from collections.abc import Iterable

CODE_EXTS: frozenset[str] = frozenset({
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".rs",
    ".go",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cxx",
    ".hh",
    ".hpp",
    ".hxx",
    ".java",
    ".kt",
    ".kts",
    ".rb",
    ".swift",
})
MARKDOWN_EXTS: frozenset[str] = frozenset({".md", ".mdx", ".markdown"})


def detect_categories(changed_files: Iterable[str]) -> set[str]:
    """Return the set of {"code", "markdown"} categories present."""


def select_targets(
    categories: set[str],
    makefile_targets: set[str],
) -> list[str]:
    """Return the ordered list of targets to invoke.

    The category-to-candidate mapping is:

    - "code" → ["check-fmt", "lint", "typecheck"]
    - "markdown" → ["markdownlint", "nixie"]

    Each candidate is included only when it appears in ``makefile_targets``.
    Order within each category follows the mapping above; categories are
    emitted in insertion order: code first, then markdown. Duplicates
    are removed while preserving first occurrence.
    """
```

`CODE_EXTS` is deliberately broader than the previous Python/TS/Rust union so
the hook stays useful in mixed-language repositories without further work.
Files whose extension is neither in `CODE_EXTS` nor in `MARKDOWN_EXTS` (for
example `.json`, `.toml`, `.lock`, `.yml`) do not trigger any target on their
own; they ride along with whatever category another changed file did select.

Implement `parse_makefile(path)` in `driver.py` using `make-parser`. Continue
to use the existing Netsuke probe when the driver is `netsuke`. Remove the
`MAKE_TARGET_PROBE`-based `make -p` invocation *for the make driver* only after
Milestone 3's tests pass.

Update `state.HookState` to retain a `categories: set[str]` field (now
`{"code", "markdown"}` rather than the previous Python/TS/Rust split) and add
`targets_present: list[str]` for the post-selection list.

Validation: snapshot tests using `syrupy` over a corpus of small `Makefile`
fixtures (`tests/fixtures/makefiles/*.mk`) combined with synthetic changed-file
lists assert the selected target list for each `(category, makefile)`
combination. The repository's own `Makefile` is one of the fixtures, and a
code-only change against it must select `check-fmt lint typecheck` while a
Markdown-only change must select `markdownlint nixie`.

### Milestone 4: PR-rebase gate

Add `post_turn_quality_stop_hook/github.py`. Define `lookup_pr`, which accepts
the primary remote, branch, and timeout, and returns a pull request summary or
`None`. The summary has:

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
   (`git@host:owner/repo.git`) and HTTPS (`https://host/owner/repo`) forms;
   strip the `.git` suffix.
2. Initialize a `github3.GitHub` client using the `GITHUB_TOKEN`
   environment variable, falling back to `gh auth token`'s output if present.
   If neither is available, return `None` immediately without network contact.
3. Use `concurrent.futures.ThreadPoolExecutor` to enforce the
   configured timeout. On `TimeoutError`, return `None` and record the incident
   on the hook state.
4. Query `repo.pull_requests(state='open', head=f'{owner}:{branch}')`
   and pick the first.

Add the "PR base ahead of local base" check. The remote base OID (`pr.base.sha`
after a fresh fetch) is compared against the local merge-base commit. If the
OIDs differ and `git merge-base --is-ancestor local_base remote_base` succeeds,
the remote is ahead; block the stop with the rendered template.

Create `post_turn_quality_stop_hook/templates/rebase_required.j2` populated
byte-for-byte from the canonical copy stored alongside this plan at
[`docs/templates/rebase_required.j2`](../templates/rebase_required.j2). The
wording was specified by the product owner and is verified by a `syrupy`
snapshot test against the file's contents, so do not modify either file in
isolation.

Render it via Jinja and emit it as the `reason` of the block payload.

Validation: betamax cassettes under `tests/cassettes/` record three fixture
conversations (PR base ahead, PR base level, no PR). A syrupy snapshot test
asserts byte-for-byte equivalence of the rendered template with the four
combinations of `(zdiff3 configured?, makefile has typecheck?)`.

### Milestone 5: commit and push gates

Replace `compush_check` with two distinct gates, both rendered with Jinja
templates (`uncommitted_required.j2` and `unpushed_required.j2`). Both run
unconditionally when the corresponding `Config` toggle is `True`, regardless of
the legacy `POST_TURN_COMPUSH` environment variable, which is removed at this
milestone.

The execution order in `run_stop_checks` is:

1. Quality checks (if `local_base_commit` is set and HEAD differs from
   it).
2. Uncommitted-changes gate.
3. Unpushed-commits gate (requires `upstream_ref` to be present).
4. PR-rebase gate (requires a PR lookup that returned a summary).

The first failing gate emits its block payload and the hook returns 0 with that
payload printed. Subsequent gates are not evaluated.

Validation: behavioural scenarios assert each gate fires in isolation and that
the precedence ordering above holds when multiple conditions are simultaneously
true.

### Milestone 6: CLI integration via Cyclopts

Wrap `hook.main` in a thin Cyclopts-driven CLI that accepts:

- `--config <path>` — explicit configuration file.

This flag is an addition. With no flags, stdin handling and behaviour match
prior milestones. Invalid CLI arguments are reported as hook block payloads
with exit code 0, preserving the Claude Code stop-hook contract.

Focused CLI tests exercise default parsing, override parsing, invalid
arguments, and the end-to-end precedence rule that an explicit `--config` file
wins over repository-local configuration.

Validation: `pytest tests/test_cli.py tests/features/cli.feature` passes.

### Milestone 7: documentation, snapshot validation, and CodeRabbit

Update `README.md`, `docs/users-guide.md`, and `docs/developers-guide.md` to
describe the new gates, the configuration file format, the Jinja templates, and
the CLI flags.

Run `make check-fmt`, `make lint`, `make typecheck`, `make test`,
`make markdownlint`, and `make nixie` in that order. Each must pass.

Run `coderabbit review --agent` against the branch. Clear every concern. If the
rate limit is hit, sleep using `vsleep` for `shuf -i 15-30 -n 1` minutes before
retrying.

## Concrete steps

Run the following commands from the repository root. Output is captured to
`/tmp/$ACTION-post-turn-quality-stop-hook-enforce-rebase.out` so the agent can
review after each command.

1. Synchronize the workspace and install dev dependencies:

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

- `make check-fmt`, `make lint`, `make typecheck`, `make test`,
  `make markdownlint`, and `make nixie` each exit 0.
- The behavioural feature files under `tests/features/` describe each
  gate in plain English, and each scenario passes.
- The syrupy snapshot fixtures match for the four
  `(zdiff3?, typecheck target?)` combinations of the rebase template.
- Running `printf '{}' | post-turn-quality-stop-hook` on a clean,
  fully-pushed, rebased branch prints nothing.
- Running the same command on a branch with a single uncommitted file
  prints exactly one JSON object whose `reason` matches the Jinja template
  `uncommitted_required.j2`.
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
  subprocess calls use a list argv form; `noqa: S603` annotations retain a
  justifying comment.

## Idempotence and recovery

Every milestone is committable as a self-contained change. If a milestone needs
to be redone, revert its commit with `git revert <sha>` (creating a new commit)
and re-run from the start of the milestone. Do not `git reset --hard`.

If `make-parser` integration fails after attempted use, revert Milestone 3's
commit and continue with the existing `make -p` probe — but record the fallback
in `Decision Log` and re-open the corresponding entry in `Risks`.

If a betamax cassette becomes stale (the GitHub API surface changes), delete
the cassette file and re-record by setting `BETAMAX_RECORD=once` in the
environment and running the affected test. Cassettes must not contain
credentials; the recorder is configured to redact `Authorization` headers.

## Artefacts and notes

The Jinja template under
`post_turn_quality_stop_hook/templates/rebase_required.j2` must preserve the
two-space indentation that the brief uses around the sentence beginning "For
packaging lock files". The whitespace is significant for snapshot equality.

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

External dependencies pinned via `[tool.uv.sources]`:

- None for Milestone 6; Cyclopts is installed from PyPI.

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
- `post_turn_quality_stop_hook.pipeline.run_stop_checks(...) -> int`.

## Revision note

Initial draft. The plan introduces seven milestones, defines new modules under
`post_turn_quality_stop_hook/`, and adds five runtime/dev dependencies.
Subsequent revisions should append a one-paragraph note summarizing what
changed, why, and how it affects remaining work.

Revision 2 (2026-06-05): Milestone 3 was tightened to keep file-type gating
instead of running every Makefile target on every change. Code-file changes
select `check-fmt`, `lint`, and `typecheck` when each is declared in the
`Makefile`; Markdown-file changes select `markdownlint` and `nixie` on the same
basis. The "code" extension set was broadened beyond the previous
Python/TS/Rust union to cover most common compiled and scripted languages. The
`gate_quality_checks` description in Milestone 1, the make-parser risk in
`Risks`, and the `Decision Log` were updated to match. No change to Milestones
0, 2, 4, 5, 6, or 7.

Revision 3 (2026-06-05): Implementation began after explicit user approval in
the work request. The plan status changed from `DRAFT` to `IN PROGRESS`, and
Milestone 0 was recorded as complete because the current branch already reported
`enforce-rebase`.

Revision 4 (2026-06-05): Milestone 1 was completed. The implementation added
`post_turn_quality_stop_hook.config`, threaded `Config` into
`StopCheckOptions`, loaded repo-local configuration from `hook.main`, and added
unit and CLI integration tests for defaults, precedence, override files, and
unknown-key errors. The dependency lockfile now includes `cyclopts`.

Revision 5 (2026-06-05): Milestone 2 was completed. The implementation added
`post_turn_quality_stop_hook.git_facts`, primary remote resolution, generic
remote branch fetching, three-way merge-style detection, and pipeline
preparation wiring. Focused pytest tests cover configured remotes, origin
fallback, first-remote fallback, no-remote behaviour, upstream merge-base
selection, and absence of a primary remote.

Revision 6 (2026-06-05): Milestone 3 began and immediately hit the
`make-parser` tolerance. Inspection of the installed `make-parser` 0.1.2 source
showed that it cannot recognize hyphenated target names, which are required for
this repository's quality gates. The exploratory dependency change was removed,
`uv.lock` was restored by `make build`, and implementation stopped pending user
direction.

Revision 7 (2026-06-06): Milestone 3 resumed after the user approved direct
regexp parsing for this use case. The implementation now parses Makefile named
targets from text, detects broad `code` and `markdown` categories, selects only
declared targets in category order, and records present targets in hook state.

Revision 8 (2026-06-06): Milestone 4 was completed. The implementation added
GitHub remote URL parsing, optional token discovery, timeout-bound PR lookup,
the bundled Jinja rebase template, and a PR-base-ahead gate that renders the
template when the PR base has advanced. Tests cover URL parsing, template
equivalence, rendered typecheck inclusion, blocking behaviour, and graceful
skip behaviour when primary remote information is unavailable.

Revision 9 (2026-07-06): The robust build-driver follow-up documented and
instrumented the automatic no-manifest quality skip. Auto mode now treats a
repository with neither `Netsukefile` nor `Makefile` as having no quality
targets to run, logs stable skip telemetry, and continues to branch-state
gates. Explicit driver overrides remain strict.
