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

1. Parse CLI arguments. An invalid command line is printed before any log work
   and returns `0`, so the response never waits on the log's Git queries.
2. Create the per-invocation decision log, install its signal handlers, and
   write its `start` record before the hook input, the repository, or the
   configuration is read.
3. Read hook input and load the configuration, then apply the configured log
   opt-out or retention window and resolve the remaining environment values.
4. Resolve the repository root from the hook working directory.
5. Collect best-effort Git facts, including primary remote, upstream ref,
   configured upstream remote and fully qualified merge ref, and merge-conflict
   style.
6. Ensure the configured base ref is resolvable.
7. Compute changed files from the merge-base with `HEAD`.
8. Select a build driver.
9. In automatic driver mode, skip repository quality targets when neither
   `Netsukefile` nor `Makefile` exists.
10. Map file categories to Make or Netsuke targets that are actually declared.
11. Run available quality targets and emit a Claude Code blocking response on
    failure.
12. Run branch-state gates for uncommitted changes, unpushed commits, protected
    branch commit and push avoidance, and PR rebase requirements.

`pipeline.py` orchestrates these modules; none of them holds the decision log
or writes standard output on its own:

- `quality_targets.py` detects change categories, reads the driver's declared
  targets through the stage's enumerator, and resolves the requested targets
  into the present, skipped, and grouped lists. `quality_execution.py` runs
  those groups through the stage's command runner. `quality_stage.py` holds the
  ports and limits both units share, and `quality.py` composes them into an
  evaluation and reports the selection through `PipelineObserver`. The stage
  selects no driver, builds no gate decision, renders no block, and holds no
  logger of its own: even the target-enumeration warning goes through
  `observation.py`. Its build-target runner and target enumerator are
  keyword-only ports, so a unit test decides the gate from prepared answers
  without a subprocess.
- `branch_gates.py` owns the branch-state and pull-request rebase decisions. It
  imports no adapter: the Git questions arrive through the `BranchState` port,
  the pull-request lookup and build-target reader arrive as callables, and a
  block names the template and domain facts it would render. It returns
  `BranchStateGateDecision` values, renders no payload, and writes nothing, so
  a prepared port exercises a decision directly. The one command step,
  `refresh_upstream_head`, collects the comparison the unpushed gate decides
  from.
- `pipeline.py` owns preparation, driver selection, the order the gates run in,
  and the emission that follows each decision, and it binds the gate ports'
  zero-argument callables from the `BranchGatePorts` the entry point supplies.
  It also wires the concrete runner and enumerator. Every function that reports
  takes the run's observer as a required argument, so no caller silently drops
  an outcome.
- `observation.py` is the pipeline's reporting seam. It turns a gate decision,
  a quality outcome, a stage boundary, a command, or a fixed warning into
  observer events and bounded telemetry, and it holds no logger:
  `PipelineObserver` is the pipeline's only telemetry channel. The pipeline
  itself makes no direct observer call, so adding, removing, or reordering a
  gate cannot change how an event is recorded.
- `formatting.py` renders every blocking payload, including the rebase and
  uncommitted blocks a gate names by template, and the unpushed block with the
  upstream label its message names. `emit_block_payload` is the one emitter of
  the hook's JSON, so the config-error and early-CLI blocks do not assemble a
  payload in `hook.py`.
- `repository_facts.py` observes the best-effort repository facts the durable
  record keeps, and withholds the unpushed count unless the checkout still
  names the branch and commit the record reports. It reads through the
  `RepositoryFactsSource` port; `repository_facts_git.py` is the Git adapter
  the entry point supplies for it.
- `hook_composition.py` owns the decision-log lifecycle for one invocation, and
  its `ProcessSnapshot` reads the process environment, home and working
  directory exactly once. The start directory is resolved from that snapshot,
  so a mid-run change cannot split one invocation's settings and no later unit
  reads `Path.cwd()` again.
- `recording.py` defines `InvocationLog`, the recorder the run hands to the
  pipeline, and `DecisionRecorder`, the protocol a recorder satisfies, so
  record collection never depends on where records are stored.
- `fetch_observation.py` adapts the pipeline observer to the Git helpers'
  fetch-observer protocol, so an actual `git fetch` is recorded as a blocking
  command.
- `observer.py` defines `PipelineObserver`, which the orchestration boundary
  builds once and passes to every other unit. It carries both the domain events
  a decision depends on and the bounded operator telemetry (`gate_telemetry`,
  `warning`). Neither implementation writes a stream itself: `NullObserver` and
  `RecorderObserver` receive the `warn` callable the entry point binds, so a
  run with logging disabled reports a warning exactly as a recording run does.
- `diagnostics.py` holds `warn`, the one best-effort standard-error write, so a
  missing or broken stream costs a warning rather than the JSON decision.
- `process.py` supervises subprocesses, and its `LaunchedProcess` result keeps
  whether a command actually started apart from the status it returned.
- `git.py` reads repository state and performs fetch and ref operations, and
  its `FetchObserver` protocol lets a caller observe only fetches that really
  start. `head_commit` reads the commit a record reports. The location split
  answers the questions the decision log asks before writing or deleting a file:
  `git_location_paths.py` holds the path arithmetic and the rules that decide
  which worktree a path belongs to, `git_location_query.py` runs the Git
  commands from one environment snapshot and one injected runner, and
  `git_location.py` composes both into the tri-state ownership answers. The
  index queries clear the inherited `GIT_INDEX_FILE`, and the tracked-file
  query also clears `GIT_LITERAL_PATHSPECS`: the query writes its own
  `:(literal)` magic, which that global switch would turn into part of the file
  name. The reported worktree path loses only Git's terminating newline, so a
  checkout whose name ends in a space keeps the identity Git reported.
- `state.py` carries the frozen reporting snapshot and the immutable stage
  results; `PreparationBlock.reason` is the fixed, output-free explanation its
  block records, which keeps a failure's message out of the durable log.
- `decision_log/composition.py` is the one named place that binds the real Git
  location queries to the decision-log adapters, so no constructor holds a
  silent Git default. `location_queries` builds the adapter from an explicit
  environment snapshot, and the `default_jsonl_sink` factories bind it together
  with the system clock and the standard-error writer the sink requires. Its
  `default_jsonl_sink_for` factory is what a composition root uses to build the
  sink a log writes through.
- `decision_log/adapters.py` exports the adapter-facing names
  (`JsonlRecordSink`, `LockedJsonlAppender`, `MeasuredRecordSink`,
  `SignalGuard`, `default_jsonl_sink`, `default_jsonl_sink_for`,
  `prune_old_logs`, `prune_with_defaults`). `decision_log/__init__.py` exports
  only the domain-facing names, so importing a domain unit loads no Git query
  and no filesystem adapter.
- `decision_log/sink.py` appends and prunes validated records and keeps no
  telemetry. It returns the lock wait an append incurred and lets a storage
  failure escape. `decision_log/telemetry.py` is the `MeasuredRecordSink`
  wrapper that times and counts those calls, so either the storage unit or the
  measurement can be substituted without the other. Retention scheduling stays
  in `decision_log/schedule.py`, which asks the wrapper to prune once the
  configured window is known.
- `decision_log/clock.py` supplies `Clock` and `SystemClock`, the time source
  stage and record timings are measured with.
- `decision_log/constants.py` holds the schema version, file naming, record
  types, gate order, and the size bounds every record respects.
- `decision_log/records.py` defines the record shapes and the recorder and sink
  protocols, and bounds the free-text and payload-derived values a record may
  carry.
- `decision_log/stats.py` accumulates bounded storage timings and failure
  categories, so an operator can tell a slow or contended log from a failing
  one. It is a plain collector: the measurement wrapper feeds it and
  `DecisionLog` reads it, so no storage unit owns it.

The decision log has two configuration keys, `log_enabled` (a TOML boolean,
default `true`) and `log_retention_days` (a non-negative integer, default `14`).
[The users' guide](users-guide.md) documents their behaviour, including the
environment overrides and retention rules.

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
- `refresh_upstream_head` in `branch_gates.py` is the command step. It settles
  the preflight outcomes and asks the injected port for a comparison, logging a
  failed one. It returns either a terminal gate decision or an
  `UpstreamComparison`. The gate module imports no adapter; `hook.main` is the
  composition root and supplies an `UpstreamSourceFactory`, which
  `run_stop_checks` calls before `run_branch_state_gates`. A caller can
  therefore substitute another port without the decision path importing the Git
  adapter.
- `unpushed_commits_gate` decides only from the collected `UpstreamComparison`
  or terminal decision. It performs no network access, writes no refs, renders
  nothing, and logs nothing, so a prepared comparison can exercise the block
  and fail-open paths directly. `formatting.emit_unpushed_block` renders and
  emits the payload after the gate has decided to block;
  `formatting.upstream_label` is the presentation rule that turns the
  comparison's fields into the displayed target.
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

The protected-upstream skip is read from the same snapshot as the comparison.
It therefore applies only while `GitFacts.branch` still names the checked-out
branch: a checkout that moved since collection would otherwise let another
branch's protected upstream suppress this branch's gate, so the gate compares
instead of skipping. The live local protected-branch check is unchanged.

The pull-request gate captures the checked-out branch and commit when it looks
the pull request up. The merge base is computed against that commit rather than
live `HEAD`, and a pass or block is reported only while the captured branch and
commit are still checked out; a checkout that moves during the check records a
`skip` with a fixed reason instead.

The `unpushed_commits_refresh_upstream` setting turns the network refresh off.
The two upstream kinds then behave differently:

- A `"."` remote names a local branch. It is already authoritative and is
  compared against the configured merge ref with or without refresh enabled; no
  fetch is attempted either way.
- Any other upstream is compared against its local remote-tracking ref. That
  ref can be stale after an explicit-URL push. If no local ref exists, as when
  `branch.<name>.remote` is a URL that was never fetched, the gate passes with
  a warning rather than guessing a comparison.

### Branch-gate ports

The branch-state and pull-request rebase gates decide from three injected
adapters, all bound once at the composition edge:

- `branch_state_port.py` declares the `BranchState` protocol: the current
  branch, `HEAD`, `ensure_remote_branch_ref`, `has_uncommitted_changes`,
  `merge_base`, `is_ancestor`, and `remote_url`. Each method returns the Git
  helper's own `(value, HookError)` answer, so the gate reads a value and never
  imports the adapter.
- `branch_state_git.py` implements that protocol by delegating to `git.py`.
  `hook.main` builds `GitBranchState` and passes it in the `BranchGatePorts`
  bundle that `run_stop_checks` forwards to the gates.
- The pull-request lookup arrives as a `PullRequestLookup` callable whose
  timeout is bound where the configuration is authoritative, so the gate never
  reads `github_timeout_seconds`. `pull_request.py` holds the
  `PullRequestSummary` value; `github.py` implements the lookup and re-exports
  the value for its own callers.
- The build-target reader arrives as a `BuildTargetEnumerator` callable that
  returns the declared targets or the failure. `pipeline.pr_rebase_check` binds
  the repository and Make driver before calling the gate, so `branch_gates.py`
  imports neither `driver.py` nor a target enumeration, and the typecheck hint
  and its warning stay derived from one read.

`upstream_label.py` holds the display rule the unpushed warning and payload
share, so the gate does not import `formatting.py`. A block carries a
`BlockPayload` of template name and domain facts;
`formatting.emit_decision_payload` renders it through the one payload emitter.
`tests/test_branch_gate_boundaries.py` proves in a fresh interpreter that
importing the gate module loads no Git, GitHub, driver, formatting, template or
subprocess module.

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

Quality evaluation is split by responsibility: `select_categories` detects
categories and requested targets and `enumerate_quality_targets` resolves
targets against the selected driver, both in `quality_targets.py`;
`execute_quality_targets` runs the grouped targets in `quality_execution.py`;
and `evaluate_changes` composes enumeration and execution from a caller-supplied
`CategorySelection` in `quality.py`. The ports and limits those units share
live in `quality_stage.py`, and the pipeline imports `select_categories`,
`evaluate_changes`, and `evaluation_report` for orchestration. Stage records
keep category sets as `frozenset` so callers cannot mutate a returned
evaluation. `run_quality_checks_if_needed` returns either a snapshot carrying
the failure or the report built by `evaluation_report`, and `run_stop_checks`
emits it through `block_and_print`. The quality stage therefore decides
*whether* to block without writing to stdout; the orchestrator remains the only
emitter for quality failures.

The pure category and target rules live in `quality_policy.py`, which imports
no process module. `quality.py` reaches build tools only through the ports on
`QualityStage`; `pipeline.py` supplies `get_build_targets` and
`run_build_targets` when it builds that stage. The direct quality-stage tests
pass fake ports; pipeline-level tests still patch the pipeline's adapter
bindings at that boundary.

`run_quality_checks_if_needed` publishes the requested targets as soon as the
changed files select them, so a stop during driver selection still records what
the change asked for, with the driver and enumerated targets left empty. Its
gate outcome comes from one helper: a `pass` is recorded only when a requested
target actually ran, and an enumeration that declares none of the requested
targets records a `skip` with a fixed reason instead. A build group whose
executable never started records the shared launch-failure value rather than
its synthetic `127`, mirroring `TargetEnumeration.recorded_exit_status`.

The build-driver boundary is represented by `BuildDriver` and
`BuildTargetRequest`. Driver-specific behaviour is deliberately narrow:

- target discovery is handled by `get_netsuke_targets` and `get_make_targets`,
- command construction is handled by `build_command`,
- execution is handled by `run_build_targets`.

A target enumeration returns a `TargetEnumeration`: the declared targets or the
`HookError` that prevented reading them, together with the enumerating
command's own exit status. `netsuke generate` carries the status its child
returned, or the shared launch-failure value when the executable could not be
started; a Makefile read runs no command and records none. `evaluate_changes`
reports one warning for a quality-gate failure through the run's
`PipelineObserver`, and `pr_rebase_check` reports its optional rebase hint the
same way: neither unit writes the standard-library logger. Quality execution
also emits the existing structured block reason. If enumeration fails while
preparing that rebase hint, the hook keeps the rebase response and omits the
unverified typecheck hint.

Keep new driver support inside that boundary. Avoid spreading driver-specific
conditionals across the stop-check pipeline.

`parse_env` accepts an optional `environ` mapping so tests can supply an
isolated environment. Omitting it, or passing `None`, reads `os.environ`, which
keeps a direct caller working without a composition step. `main` does not rely
on that default: it passes the per-invocation `process.environ` snapshot
explicitly, so the base ref and the runtime options both come from the same
reading of the environment and a value the process changes mid-run cannot split
one invocation's settings.

Subprocesses run by the hook receive a sanitized environment: `_subprocess_env`
in `post_turn_quality_stop_hook/process.py` enriches `PATH`, removes
`FORCE_COLOR` and `CLICOLOR_FORCE`, and sets `NO_COLOR=1`. The `Makefile`
exports the same setting for gate runs. The tool directories added to `PATH`
are resolved under the home the supplied environment snapshot names, so a child
sees a `PATH` that describes the same home its own `HOME` does; a caller that
supplies no snapshot keeps the live home.

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
silent, report a bounded `quality_gate_skip` telemetry event through the
observer, and then continue to branch-state gates. Explicit
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
should report bounded structured context through `PipelineObserver`, such as
the gate, outcome, matched branch, and whether the match was local or upstream.
The quality gate reports its own outcomes under `quality_gate_skip` and
`quality_gate_block`, never as a branch-state gate.

### Decision log

The durable per-invocation log lives in
`post_turn_quality_stop_hook/decision_log/`. It separates the concerns so each
can be tested without the others:

- `settings.py` resolves `log_enabled` and the retention window from the
  configuration and the environment, plus the XDG state directory from an
  injected `environ` and `home` so no unit holds a hidden process global.
- `log.py` orchestrates one run's records through the `RecordSink` it is given;
  `assembly.py` turns the collected facts into the `start`, `progress`, and
  `finish` records. The log never builds a sink: the composition root supplies
  one, so the domain has no storage or Git dependency. The clock, run-ID
  factory, process id, signal-name table, and terminal-commit mask are required
  collaborators, so the domain reads no clock, process, or signal state itself.
- `sink.py`, `append.py`, `policy.py`, and `platform_support.py` perform the
  locked single write, and decide whether a daily file may be used at all. The
  append adapter owns every descriptor operation: it asks the injected
  `FileOwnership` port for a claim and hands the policy that value, so
  `policy.py` never stats or reads a file. `WritePolicy` requires its
  `on_error` channel, which only a composition root binds. `sink.py` keeps no
  telemetry: `telemetry.py` wraps it in `MeasuredRecordSink`, which times and
  counts each append and prune into the shared `StorageStats` collector.
- `ownership.py` recognizes the files the hook wrote, including the
  `DescriptorClaim` and `DescriptorOwnership` adapter behind the policy's
  ownership port, and `retention.py` deletes only expired files it can prove it
  owns.
- `schedule.py` is the domain half of retention: it applies the resolved window
  to the run's sink, while `retention.py` performs the filesystem sweep behind
  injected ownership queries. The append path never schedules retention.
- `signals.py` records interruptions and masks catchable signals around a
  terminal record. It also supplies `signal_name`, the platform's signal-name
  table, which the composition root binds into the log.
- `clock.py`, `constants.py`, `stats.py`, and `records.py` supply the time
  source, the schema and size constants, the bounded storage counters, and the
  record, sink and error shapes those units share. `bounds.py` bounds the free
  text and payload-derived values a record may carry, and `timing.py` holds the
  pure elapsed-time arithmetic, so a domain unit can use both without importing
  the clock or the record-shape module.
- `composition.py` binds the real Git location queries once, so no adapter
  constructor holds a silent default.
- `adapters.py` is the adapter-facing export surface, including the retention
  sweep (`prune_old_logs`), the signal guard, `signal_name`, and
  `terminal_commit`; the package `__init__.py` exposes the domain-facing names
  only.

The domain boundary is enforced, not merely documented.
`tests/test_decision_log_boundaries.py` imports the log and policy in a fresh
interpreter and proves that neither reaches the Git, storage, clock, signal,
record-shape, or diagnostics modules. A second check parses those sources and
proves they never import or call `os`, `sys`, or `signal`, which is what keeps
`os.getpid`, `sys.stderr`, and the platform signal table at the composition
root.

The pipeline never holds the log. `run_stop_checks` reads the optional
`InvocationLog` recorder once and builds a `PipelineObserver` from it; with
logging disabled that is a `NullObserver`, so a gate sees the same protocol and
records nothing. Every gate outcome, quality fact, blocking command, and fixed
warning goes through that one channel, and `observation.py` is the module that
speaks it. The `warn` callable those observers write a warning through is bound
by the entry point and passed on by `run_stop_checks`, so neither observer
implementation imports the standard-error writer. `repository_facts.py`
collects the log-only facts through the same observer, reading them through the
`RepositoryFactsSource` port so the observation unit imports no Git. Gate
functions therefore never import the log, and a substituted observer can assert
the decisions a gate reported.

Logging must never change the decision. The log is written only outside every
worktree, or where Git confirms it ignores the path; the sink refuses any other
location for the rest of the run, so an unignored file inside a repository can
never appear as a change the uncommitted-changes gate reports. A write, lock,
or retention failure is reported on standard error and never reaches the JSON
decision. [ADR-003](adrs/adr-003-per-invocation-decision-log.md) records this
decision, its alternatives, and its consequences.

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

`pyproject.toml` carries typed entry-point declarations under
`[[tool.skylos.dead_code.entrypoints]]` for the decision-log call paths,
because the pinned scan cannot follow the seams those paths run through. There
are five groups of declaration, each for a different reason:

- The unpushed-upstream adapters are reached through the `UpstreamHeadSource`
  protocol, which the scan does not model, so `upstream.py`'s fetch chain and
  `GitUpstreamHeadSource`'s methods have no static caller.
- `RetentionSchedule.apply` is invoked through the `retention` attribute the
  composition edge reads once the configured window is known, which the scan
  sees as an attribute rather than a call.
- `ObserverFetchObserver` implements the Git helpers' `FetchObserver` protocol,
  so its two callbacks are reachable only through that protocol.
- `DecisionLogRun`'s `stage`, `set_correlation`, and `finish` are used on the
  value a `with` statement binds, which the scan does not follow.
- `DescriptorClaim.holds_hook_records` and `DescriptorOwnership.claim` implement
  the write policy's ownership port, so the policy and the append adapter reach
  them only through Protocol-typed values.

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
Netsuke CLI contract test additionally requires the released `netsuke-build`
v0.1.0-beta1, which CI installs with `cargo-binstall`. Every job that runs the
suite, through the generate-coverage action or a direct `pytest` or
`make test`, installs that release in an unguarded step before the tests run:
the pull-request lane in `ci.yml` and the publisher in `coverage-main.yml`.
`tests/netsuke_install_rules.py` holds that rule, with the version named in the
rule rather than read from a workflow, and
`tests/test_netsuke_install_contract.py` proves each clause by mutation. Jobs
that call a reusable workflow run their steps elsewhere and are outside the
rule.

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
- the decision-log domain import boundary in a fresh interpreter, proving
  importing `decision_log.log` and `decision_log.policy` loads neither the Git
  queries nor the storage adapters,

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
