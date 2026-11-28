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
   files, then hand the remaining steps to the consecutive-failure breaker in
   `post_turn_quality_stop_hook/failure_breaker.py`.
2. Resolve the repository root from the hook working directory.
3. Collect best-effort Git facts, including primary remote, upstream ref,
   configured upstream remote and fully qualified merge ref, and merge-conflict
   style.
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

### Unpushed-upstream verification

The unpushed-commits gate must compare `HEAD` with the remote's actual head,
not with the local remote-tracking ref, because `git push <url>` moves the
remote branch without updating `refs/remotes/<remote>/<branch>`. The pipeline
splits that work into a command step and a pure decision function:

- `upstream_port.py` holds the domain-facing types: the `UpstreamHeadSource`
  protocol and its `UpstreamComparison` value object. Its single method,
  `refresh_comparison`, is the only call that performs transport work; the gate
  reads the returned fields and nothing else. The module imports no Git,
  process, subprocess, or redaction module, so the decision path cannot reach
  transport code through it.
- `upstream.py` holds the Git adapter, `GitUpstreamHeadSource`. It reads the
  upstream from `GitFacts`, resolves whether that upstream is a remote name, a
  URL, or the local `"."` remote, fetches the configured branch with
  `fetch_upstream_head`, and compares the fetched head with `HEAD` using
  `has_unpushed_commits`. It returns the comparison through the port.
- `refresh_upstream_head` in `pipeline.py` is the command step. It settles the
  preflight outcomes and asks the injected port for a comparison, logging a
  failed one. It returns either a terminal gate decision or an
  `UpstreamComparison`. The pipeline imports only `upstream_port`; `hook.main`
  is the composition root and supplies an `UpstreamSourceFactory`, which
  `run_stop_checks` calls before `run_branch_state_gates`. A caller can
  therefore substitute another port without the decision path importing the Git
  adapter.
- `unpushed_commits_gate` decides only from the collected `UpstreamComparison`
  or terminal decision. It performs no network access, writes no refs, renders
  nothing, and logs nothing, so a prepared comparison can exercise the block
  and fail-open paths directly. `_emit_unpushed_block` renders and emits the
  payload after the gate has decided to block; `_upstream_label` is the
  presentation rule that turns the comparison's fields into the displayed
  target.
- `resolve_upstream` in `git.py` reads `branch.<name>.remote` and the first
  `branch.<name>.merge` value. `@{u}` cannot represent a URL-valued remote, and
  Git's `--get` returns the last merge value while `@{u}` uses the first.

`fetch_upstream_head` in `upstream.py` fetches the fully qualified source ref
into a unique private ref under `refs/post-turn-quality/fetch/`, then reads
that ref with `git rev-parse --verify <ref>^{commit}`. The private ref isolates
the result from a concurrent fetch that replaces the shared `FETCH_HEAD`, and
the fully qualified source avoids resolving a same-named tag instead of the
branch. The fetch passes `--no-write-fetch-head`, so the shared file keeps
whatever the developer's own commands left there, and `--refmap=`, so the
explicit destination ref is the only mapping Git applies. Without the cleared
refmap, a verification fetch could advance `refs/remotes/<remote>/<branch>` and
silently reset the expected value for a later `git push --force-with-lease`.
`--no-recurse-submodules` keeps the result dependent only on the superproject
head the gate compares.

The temporary ref is deleted in a `finally` block with a bounded timeout, so a
stalled `reference-transaction` hook cannot hang the stop hook. Deletion is
best effort: a repository hook may reject it. The return code is checked and a
failure is logged with the ref name and a redacted reason, because every later
run uses a fresh ref and would never retry the cleanup. A failed deletion does
not invalidate a comparison that already succeeded. The ref-name factory and
command runner are injectable for tests.

The adapter reads the checkout branch and HEAD either side of the fetch. If the
branch has changed since `GitFacts` was collected, or HEAD moved while the
fetch ran, the comparison is abandoned with an error instead of comparing one
branch's commits against another branch's upstream. That path fails open, like
any other unverifiable comparison. Fact collection snapshots the branch before
it reads the upstream, so a checkout that moves in between leaves the older
branch in the facts and the adapter's liveness check turns that staleness into
a refusal.

When resolution or the fetch fails, the gate fails open with a warning instead
of blocking on unverified data. Diagnostics are redacted before they are
logged; a configured URL that cannot be rendered safely is replaced by the
branch name alone, so a malformed credential-bearing value cannot reach a
payload or a log line. The same rule covers a Git remote-helper address such as
`https::<url>` or `ext::<command>`: the address is free-form and may itself
carry a URL, so it is redacted as a whole and withheld when any `@` remains.
Ordinary remote names, local paths, and scp-style addresses are unchanged.

The `unpushed_commits_refresh_upstream` setting turns the network refresh off.
The two upstream kinds then behave differently:

- A `"."` remote names a local branch. It is already authoritative and is
  compared against the configured merge ref with or without refresh enabled; no
  fetch is attempted either way.
- Any other upstream is compared against its local remote-tracking ref. That
  ref can be stale after an explicit-URL push. If no local ref exists, as when
  `branch.<name>.remote` is a URL that was never fetched, the gate passes with
  a warning rather than guessing a comparison.

### Subprocess timeouts

`run` in `process.py` accepts a `timeout` keyword. A timed-out command is
reported as a `CompletedProcess` with return code `124`, so callers apply their
normal error handling. Cleanup differs by platform:

- On platforms with process-group support, the command runs in a new session
  and the runner signals the whole group: `SIGTERM` first, then `SIGKILL` after
  a short grace period. Group signalling is best effort. It makes a transport
  such as SSH much less likely to outlive the timeout, but the signal call can
  fail and the wait for the group to exit is bounded.
- Where process groups are unavailable, only the direct child is signalled. A
  transport started by that child can then outlive the timeout.

Return code `124` therefore means the runner stopped waiting; on neither path
does it prove that every descendant exited. Do not describe the timeout as
leaving the command running: the direct child is always signalled, and the
group is signalled wherever the platform supports it.

`process.py` also owns the enriched subprocess `PATH`; `redaction.py` owns URL
credential removal; `git.py` keeps the Git queries and configuration reads.
Keep transport work out of gate decision functions.

The main state carrier is `HookState`, a frozen reporting snapshot that keeps
user-facing failure output consistent by collecting the diff base, changed
files, categories, selected driver, target decisions, command results, and any
preparation error in one place. The orchestrator in `run_stop_checks` owns
stdout emission and assembles the final snapshot with `dataclasses.replace`;
stage helpers never mutate an existing snapshot.

Immutability is deep, not just frozen attributes. `HookState` stores changed
files, targets, and commands as tuples and categories as a `frozenset`, and
`CommandResult` is a frozen dataclass, so neither a snapshot nor a recorded
command can change after assembly. `QualityEvaluation.blocked` is derived from
those recorded exit codes; a test that mutates a command's `exit_code` fails
rather than silently changing the result.

Each pipeline stage returns an immutable result rather than mutating shared
state:

- `PreparationReady` reports a resolved repository root and the snapshot that
  quality checks should continue from, `PreparationStop` reports a quiet
  non-repository exit, and `PreparationBlock` reports the failure together with
  the partial snapshot,
- `CategorySelection` reports detected categories and requested targets,
- `TargetSelection` reports present, skipped, and grouped quality targets,
- `QualityEvaluation` reports detected categories, driver, target decisions,
  command results, and any failure.

All fallible helpers return the shared `HookError` value from
`post_turn_quality_stop_hook/errors.py`; no helper reports a failure as a bare
string. A helper that returns a value alongside an error uses
`(value, HookError | None)`. `ensure_base_ref` and `ensure_remote_branch_ref`
return `(ok, HookError | None, fetched)`, so their error sits between the
outcome and the fetch flag; their callers unpack that order. Other multi-value
results keep the error last. `BranchProtectionCheck`,
`GitFacts.upstream_error`, and `UpstreamComparison.error` all carry
`HookError | None`, so the operation that failed survives from the Git adapter
to the decision that reports it.

Quality evaluation is split into focused units: `select_categories` detects
categories and requested targets, `enumerate_quality_targets` resolves targets
against the selected driver, `execute_quality_targets` runs the grouped
targets, and `evaluate_changes` composes enumeration and execution from a
caller-supplied `CategorySelection`. The units live in
`post_turn_quality_stop_hook/quality.py`; the pipeline imports
`select_categories`, `evaluate_changes`, and `evaluation_report` for
orchestration. Stage records keep category sets as `frozenset` so callers
cannot mutate a returned evaluation. `run_quality_checks_if_needed` returns
either a snapshot carrying the failure or the report built by
`evaluation_report`, and `run_stop_checks` emits it through `block_and_print`.
The quality stage therefore decides *whether* to block without writing to
stdout; the orchestrator remains the only emitter for quality failures.

The pure category and target rules live in `quality_policy.py`, which imports
no process module. `quality.py` reaches build tools only through the ports on
`QualityStage`; `pipeline.py` supplies `get_build_targets` and
`run_build_targets` when it builds that stage. The direct quality-stage tests
pass fake ports; pipeline-level tests still patch the pipeline's adapter
bindings at that boundary.

The build-driver boundary is represented by `BuildDriver` and
`BuildTargetRequest`. Driver-specific behaviour is deliberately narrow:

- target discovery is handled by `get_netsuke_targets` and `get_make_targets`,
- command construction is handled by `build_command`,
- execution is handled by `run_build_targets`.

Target-enumeration failures are returned as `HookError`. `evaluate_changes`
logs one warning for a quality-gate failure through the `quality` module
logger, and `pr_rebase_check` logs a warning for the optional rebase hint.
Quality execution also emits the existing structured block reason. If
enumeration fails while preparing that rebase hint, the hook keeps the rebase
response and omits the unverified typecheck hint.

Keep new driver support inside that boundary. Avoid spreading driver-specific
conditionals across the stop-check pipeline.

`parse_env` accepts an optional `environ` mapping so tests can supply an
isolated environment. Omitting it, or passing `None`, reads `os.environ` as
before; `main` relies on that default.

Subprocesses run by the hook receive a sanitized environment: `_subprocess_env`
in `post_turn_quality_stop_hook/process.py` enriches `PATH`, removes
`FORCE_COLOR` and `CLICOLOR_FORCE`, and sets `NO_COLOR=1`. The `Makefile`
exports the same setting for gate runs.

`NO_COLOR` is only a request, so it does not guarantee escape-free output.
`run_build_targets` therefore passes captured streams through `strip_ansi`,
which removes CSI, OSC, DCS/SOS/PM/APC, and short ESC sequences before
truncation. An OSC match stops at its first BEL or string terminator, so a
hyperlink does not consume the diagnostic text that follows it. Captured gate
output stays free of those escape sequences even when a recipe ignores
`NO_COLOR`, so a coloured path cannot corrupt the captured report. Sanitization
runs after the recipe exits; it cannot protect a path that the recipe itself
consumes.

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

The one deliberate exception to "print only the blocking payload" is the
consecutive-failure breaker, which prints a non-blocking `systemMessage` when
it short-circuits.

### Consecutive-failure breaker

The breaker wraps everything `hook.main` does after it resolves the working
directory, including configuration-error blocks. It follows the same split as
the upstream port: a pure policy, a storage port, and adapters that `hook.main`
composes.

- `failure_breaker.py` holds the policy and the port. `decide`,
  `action_after_reset` and `update_for` are pure functions of the stored count,
  the limit and a `CheckOutcome` (`PASSED` or `BLOCKED`), so the trip,
  reset-before-trip and update rules are tested without a filesystem or
  environment. A streak is named by an opaque `StreakId`; the policy never sees
  a path. `StreakStore` is the port: a protocol with `load`, `increment`,
  `clear` and `reset_if_tripped`, each atomic for its streak and raising
  `StreakStoreError` when storage fails. `run_with_failure_limit` takes the
  identity, the store and a callable returning a `CheckResult`, and returns
  either that result or a `ShortCircuit`. It is the fail-open boundary and the
  only place that logs breaker decisions. The module imports no file,
  environment or output code.
- `streak_store.py` holds the file-system adapter, `FileStreakStore`, with the
  JSON format, the atomic write and the per-streak lock. `directory_streak_id`
  derives the identity from a directory and `state_root` resolves the XDG
  location from an environment mapping and a home directory that the caller
  passes in. The adapter reports failures; it does not decide what they mean.
- `hook_output.py` adapts the hook's wire format. Gates emit their decisions by
  printing JSON at several sites in `pipeline.py`, so `capture_check_result`
  captures standard output around the run, forwards it unchanged, and
  translates any JSON object line whose `decision` is `block` into
  `CheckOutcome.BLOCKED`. This keeps the gates unaware of the breaker; a gate
  that adds a new emission site is counted with no further change.
  `short_circuit_message` renders the non-blocking `systemMessage`.
- `hook.main` is the composition root. It derives the `StreakId` from the
  start directory, reads `os.environ` and `Path.home()`, builds the
  `FileStreakStore`, wraps the checks in `capture_check_result`, and prints the
  short-circuit message when the policy returns `ShortCircuit`. Tests of the
  policy use an in-memory store, with Hypothesis properties over generated
  outcome sequences in `tests/test_failure_breaker_properties.py`; only tests
  that drive `hook.main` in-process request the `isolated_xdg_state` fixture.

Design decisions:

- The streak is keyed on the resolved start directory, matching the request
  scope, rather than the repository root. Files are named by the SHA-256 of
  that path. They no longer record the path: the store sees only the opaque
  identity. Files from earlier builds, which also hold a `directory` key, still
  load.
- State lives under `XDG_STATE_HOME`, not `XDG_CACHE_HOME`: deleting it changes
  behaviour, so it is state rather than cache.
- The breaker checks the stored count before running anything, so a tripped
  breaker skips expensive build targets. It resets the count when it trips, so
  checks resume on the next stop instead of staying disabled.
- Concurrent hooks in one directory are serialized per streak by an exclusive
  operating-system lock on a sibling `<id>.lock` file: `fcntl.flock` on POSIX,
  `msvcrt.locking` on Windows. The kernel drops the lock when a process exits,
  so a crash cannot wedge the breaker. The lock covers each store operation,
  not the checks, so concurrent stops still run their checks in parallel.
- A block records `increment`, which adds one to the count stored at that
  moment, not to the count loaded before the checks ran. A pass that lands
  while another run's checks are in progress therefore restarts the count
  rather than being overwritten by stale history, and overlapping blocks each
  count.
- `reset_if_tripped` checks and clears the count under one lock, so when
  several processes load the same tripped count, exactly one claims the reset
  and short-circuits. The others see the cleared count and run their checks.
- Writes still go through a temporary file and an atomic rename, so `load`,
  which takes no lock, never reads a torn file.
- Bookkeeping fails open. A load failure, including a corrupt file, counts as
  no streak, and failed writes, resets and lock acquisitions are logged, so the
  breaker never alters the decision the checks reached. `increment` replaces a
  corrupt record rather than failing on it forever.
- `StreakStoreError` messages name the operation and the error kind, such as
  `PermissionError (EACCES)`, never the OS error text, which carries the path.
  `XDG_STATE_HOME` can sit inside a working directory, so a path in a warning
  could disclose the directory the hashed identity hides.
- Streak identities are the SHA-256 of `os.fsencode(directory)`, computed by
  the adapter at the composition root. The filesystem codec round-trips
  surrogate-escaped bytes from non-UTF-8 POSIX names, and logs carry the digest
  rather than the raw path.
- A trip logs a `warning`, `failure breaker tripped`, with the limit, the
  previous count, the digest and the action. Routine increments and resets log
  at `info`, which the hook does not emit by default, so a passing stop stays
  silent.
- The breaker trips only after its own reset succeeds. If the streak file
  cannot be deleted, or another process reset it first, it runs the checks
  instead, because skipping with an uncleared count would skip every later stop
  and disable the gates permanently.

## Development environment

`pyproject.toml` sets `[tool.ty.environment] extra-paths = ["./tests"]`.
`tests/` is therefore a `ty` resolution root, so a test module can import a
sibling support module such as `quality_support` by name. `make typecheck`
checks both the package and the tests with that path in force.

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

The `Makefile` overrides and exports `NO_COLOR := 1` for all recipes. Keep that
export: `override` is what makes it survive `make -e` with an empty `NO_COLOR`
in the environment. The vendored `scripts/uv_gate.py` parses `uv cache dir`
output as a path, and a forced colour environment such as `FORCE_COLOR=1` makes
uv add colour codes to that output. The helper would then create a cache
directory whose name contains ANSI escapes inside the working tree. uv gives
`NO_COLOR` precedence over `FORCE_COLOR` and `CLICOLOR_FORCE`, so the export
keeps the path plain. Gate recipes are not interactive terminals, so losing
colour there costs nothing.

`make lint` runs Ruff followed by a blocking Skylos dead-code scan over
`post_turn_quality_stop_hook`, explicitly excluding `tests`. The CI `lint-test`
job runs the same target, then the shared-actions `df12-skylos` action. The
gate is the `df12-skylos` command from `df12-python-lints`, pinned by commit in
the Makefile (`DF12_PYTHON_LINTS_SHA`); its `skylos` extra carries the one
Skylos version, and it uses only local, non-interactive dead-code analysis. Its
behaviour and tests live in `df12-python-lints`; this repository keeps only the
configuration in `[tool.df12_skylos]` and `[tool.skylos]`.

Skylos parses source with its own runtime AST, so the tool environment runs
Python 3.14 (`python` in `[tool.df12_skylos]`). Pinning that parsing runtime
prevents phantom dead-code findings when the project uses syntax that an older
tool runtime cannot parse. `make skylos` runs the scan on its own.

Treat each finding as dead code until its runtime caller is verified. Remove
genuine dead code. Prefer a typed Skylos entry-point rule for an implicit
runtime caller. Only for a verified false positive that cannot be modelled as
an entry point, record the narrow exception and its caller with:

```bash
make skylos-allow SYMBOL=symbol REASON="verified runtime caller"
```

The target requires both variables and calls
`df12-skylos allow --symbol=<symbol> --reason=<reason>`, which validates them
and serializes the edit of `pyproject.toml` with an advisory lock. It
deliberately does not use `NAME`: Windows Subsystem for Linux (WSL) injects
that variable from the host name.

```bash
make test
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

CI installs the released Netsuke in `.github/workflows/ci.yml` and
`.github/workflows/coverage-main.yml`. `cargo install cargo-binstall` runs in a
step of its own, and only the following `cargo binstall` step sets
`GITHUB_TOKEN: ${{ github.token }}` in its `env:`. The token authenticates the
api.github.com release lookups; anonymous requests share a per-runner-IP rate
limit, and an unlucky run receives a 403, waits, and then compiles from source.
Keep the token off the `cargo install` step, whose build scripts would
otherwise inherit it, and off job and workflow scope.

## Coverage publication

Pull-request continuous integration (CI) generates Python coverage and ratchets
it against the baseline written by `coverage-main.yml`. The pull-request lane
publishes no coverage artefact, never contacts CodeScene, and never receives
`CS_ACCESS_TOKEN`, so a change in CodeScene's application programming interface
(API) cannot hold a pull request.

`coverage-main.yml` is the only publisher. On each push to `main` it refreshes
the ratchet baseline and uploads the report to CodeScene. It also runs on
demand through `workflow_dispatch`, for merges that fire no push event: a
dispatch on `main` uploads a fresh report, but the shared action advances the
baseline only on a push, so the ratchet catches up at the next push to `main`.
No `env` binds `CS_ACCESS_TOKEN`: a check step writes whether the secret is
set, from an expression evaluated before its shell runs, and the upload step
receives the token only as its `access-token` input, because the uploader is a
composite action that would pass its step's `env` to its nested steps. The
upload runs only when the token is present and the ref is `refs/heads/main`, so
a dispatch from a branch cannot publish that branch's coverage as the trunk's.
Merges made by the Dependabot automerge workflow use `GITHUB_TOKEN` and fire no
push event, so they are measured only at the next push to `main` or a manual
dispatch; this is a known exception, tracked in leynos/shared-actions#518. The
publisher's concurrency group is keyed on the ref alone and never cancels a run
in progress, so runs on `main` never overlap and a newer triggered run (push or
dispatch) replaces any pending one. A manual re-run of an older run is an
operator action that republishes that commit's coverage and baseline until the
next push supersedes it; a dispatch that replaces a pending push leaves the
ratchet baseline one commit behind until the next push, also tracked in
leynos/shared-actions#518. No other workflow a push starts, directly or through
a local call, may generate coverage outside the pull-request guard, and that
includes a workflow the publisher itself calls, so the publisher's own coverage
step is the only baseline writer. Both coverage steps select the same inputs at
the same `shared-actions` pin because the pull-request ratchet is only
meaningful against a baseline measured the same way.

`make test-workflow-contracts` holds this shape by running
`cv005-contracts check`, the shared contract library in `leynos/shared-actions`
(`packages/cv005-contracts`), from a full commit named by `CV005_CONTRACTS_REF`
in the Makefile, and CI runs it as its own step. A fix to the rules is
therefore a pin bump. The target needs `uv`, which fetches the Python 3.13 the
library runs under. The repository's parameters are in `.github/cv005.toml`: its
`repository` name and the `[selection]` inputs the baseline measures, which
the publisher's generator must carry and every pull-request lane must match.
The library's own suite proves each rule refuses the shape it exists to refuse,
so this repository keeps no copy of the readers or the refusal cases. Its rules
read every workflow a pull request can start, from its own events, reviews and
comments, a merge queue, or a push not confined to `main` or tags, following
local reusable-workflow calls, `workflow_run` chains and local composite
actions, and refuse any mention of the CodeScene host, uploader, client, or
token there. They also refuse `continue-on-error` wherever it would turn a
failed ratchet or upload green, and they read workflows strictly, so a
duplicate key is refused rather than silently resolved.

The publisher job declares `environment: codescene`. That environment admits
deployments from `main` alone and is where the CodeScene token lives, so only
the trunk publisher can read it. The shared library holds the placement: every
uploading job declares the environment, as a string or as `{name: codescene}`;
no other job declares it; and no workflow a pull request can start declares it
in any job.

## Testing strategy

Tests live in `tests/`. They use mocked subprocess calls for most Git, Make,
GitHub, and Netsuke behaviour so the suite is fast and deterministic. The
Netsuke CLI contract test additionally requires a released `netsuke-build`,
which CI installs with `cargo-binstall`. Every job that runs the suite, through
the generate-coverage action or a direct `pytest` or `make test`, installs it
in an unguarded step before the tests run: the pull-request lane in `ci.yml`
and the publisher in `coverage-main.yml`. Each install pins one exact version
with `--locked`, and every lane must pin the same version.

The workflows are the only place that names the release. The contract tests
check the shape of the install and the agreement between lanes, never a
particular version, so upgrading Netsuke means changing the version in every
lane's install step and nothing else. `tests/netsuke_install_rules.py` holds
that rule, and `tests/test_netsuke_install_contract.py` proves each clause by
mutation. Jobs that call a reusable workflow run their steps elsewhere and are
outside the rule.

The tests cover:

- dirty-tree and unpushed-commit detection,
- unpushed-gate verification against a real remote, including stale local
  tracking refs after explicit-URL pushes and URL-valued upstreams,
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
- property tests for the truncation budget and ordered deduplication in
  `tests/test_execution_properties.py`, and for build-driver selection under a
  controlled filesystem and tool availability in
  `tests/test_driver_properties.py`,
- Jinja template rendering.

`tests/quality_support.py` holds the shared quality-test fixtures, including
the command-result builder and the Git facts used by the quality tests.
`tests/test_quality_stage.py` holds the quality-stage unit tests and the
pipeline-integration tests that exercise the orchestrator's use of those
records.

Add tests at the same behavioural level as the change. For example, target
selection changes should exercise the relevant quality unit and
`evaluate_changes`; environment changes should exercise `parse_env` with an
explicit mapping; CLI changes should exercise `parse_cli_args` and `main`; hook
contract changes should assert captured standard output. Unit tests should
cover the split helpers directly, while integration tests through
`run_stop_checks` verify that the orchestrator emits the expected blocking
response.

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

## Running uv through the helper

Every `uv` call in the Makefile except the spelling builder's goes through
`scripts/uv_gate.py`, a copy of the helper in
[shared-actions](https://github.com/leynos/shared-actions/tree/main/uv_gate).
It cleans the environment, uses the global uv cache, runs offline first, and
goes online at most once, only when uv proves a cache entry, a managed Python
or a Git source is missing. The copy is vendored byte for byte and is never
edited in place: the concordat `uv-gate-baseline` rule audits its digest, so
the repository's file-length and scripting-stack conventions do not apply to
it, and Ruff excludes it. `make prepare` installs the locked environment, and
every gate that runs the project depends on it. A caller's `UV=/path/to/uv` is
honoured by putting that uv's directory on `PATH`, so the executable must be
named `uv`. A Git-sourced tool that is not cached yet needs uv 0.11.28 or
newer; with an older uv, running the tool once with `uvx` warms the cache. The
one exception is `make spelling`, which runs `uv tool run` directly: the
concordat `spelling-config-baseline` rule requires the builder at a release
tag, which the helper's SHA-only pin check refuses, so that call does not get
the helper's environment cleaning or offline-first policy. `uv.lock` changes
only through a manual `uv lock`; the gates never change it.

## YAML lint

`make lint` runs `make yamllint` and `make actionlint` as prerequisites.
`make yamllint` runs `yamllint --strict` over every tracked `.yml` and `.yaml`
file, reading `.yamllint.yml`, which extends the estate base
`.yamllint-estate.yml` (a verbatim copy of the concordat canon; do not edit
it). The base ignores Helm chart templates (`**/charts/**/templates/`), so a
tracked file there is passed to yamllint but not checked, and a local `ignore`
in `.yamllint.yml` replaces that entry rather than adding to it. Run it from
the repository root, because yamllint resolves `extends` against the current
directory. `make actionlint` checks the GitHub Actions workflows, accepting the
runner labels in `.github/actionlint.yaml`, and runs ShellCheck over their Bash
and `sh` `run:` scripts when it is on `PATH` (it skips PowerShell steps).

Install the tools locally at the versions CI uses:
`uv tool install yamllint==1.38.0`, and the `actionlint` 1.7.12 and
`shellcheck` 0.11.0 release binaries (a package manager's builds can differ and
change what the lint reports). CI installs the same pinned versions with the
`install-yaml-lint` action of `leynos/shared-actions` before it runs
`make lint`. The `YAMLLINT` and `ACTIONLINT` variables name the commands when
they are not on `PATH`.
