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

Target-enumeration failures are logged once at the pipeline boundary. Quality
execution also emits the existing structured block reason. If enumeration fails
while preparing an optional rebase hint, the hook logs the read error and keeps
the rebase response, omitting the unverified typecheck hint.

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

### Decision log

`post_turn_quality_stop_hook/recording.py` defines the domain-shaped
`DecisionRecorder` protocol that the pipeline and gate policy depend on.
`post_turn_quality_stop_hook/decision_log/` owns the durable per-invocation log:
`DecisionLog` collects records, `JsonlRecordSink` persists them, and
`SignalGuard` installs termination handlers. The package separates settings
resolution (`settings.py`), record-shape verification (`ownership.py`),
retention (`retention.py`), record collection (`log.py`), persistence
(`sink.py`), and signal handling (`signals.py`). Keep these invariants:

- `DecisionLog` writes a `start` record when constructed and exactly one
  matching `finish` record. Assembly and delivery are separate: `RunRecord`
  builds the record, and `DecisionLog` delivers it once. A signal that arrives
  after the record is built but before it is written therefore re-enters
  `finish` and delivers an `interrupted` record, while an ordinary second call
  from the signal handler or the `finally` block in `main` finds delivery
  closed and writes nothing.
- `stage`, `command_start`, and `command_finish` write progress immediately.
  Write progress before the protected work so `SIGKILL` still leaves evidence
  of the stage or command that was in progress. Record each branch-state gate
  as its own stage, and record every blocking tool invocation: build-target
  groups, `netsuke generate` during target enumeration, and each `git fetch`
  that really runs. `git.ensure_base_ref` and `git.ensure_remote_branch_ref`
  take a `FetchObserver` and notify it only around an actual fetch, so a
  ref-existence check is not logged as a command. Each command record carries
  the process's own exit code; `driver.get_build_targets` returns the
  `netsuke generate` status it saw, and `None` when a Makefile was parsed
  without running anything.
- The `start` record is written before standard input is read, before
  repository discovery, and before the authoritative configuration load. `main`
  starts a monotonic clock before reading the command line, and passes it to
  `make_decision_log`, so the `startup` stage and the finish record's total
  duration include that work. `SignalGuard` is installed immediately
  afterwards, before any payload or configuration read, so a catchable signal
  that interrupts one is recorded as `interrupted`. A signal that arrives
  between writing the `start` record and installing the guard cannot be caught
  and leaves a `start` record with no finish, which is the documented meaning
  of a missing finish. The `input` stage covers the payload read, and
  `DecisionLog.set_correlation` records the payload's correlation fields as
  soon as they are known. An invalid command line is answered before stdin is
  read or Git runs.
- The log is written only at a location where no decision can see it.
  `WritePolicy` asks two Git questions about today's daily file before anything
  is created: `git.is_tracked` and `git.would_appear_as_change`.
  `would_appear_as_change` answers `False` exactly when no worktree contains
  the path, or Git ignores it, and `None` when Git cannot answer. Only a
  definite `False` from each query permits a write. A tracked file, a file
  inside a worktree that Git does not ignore, and any unknown answer each
  refuse the location for the whole run: the policy warns once on standard
  error with a fixed, path-free message, and the sink creates nothing, appends
  nothing, and skips retention (`JsonlRecordSink.prune` returns without
  scanning while the policy is refused). Repository queries are never filtered,
  so `changed_files` and `has_uncommitted_changes` report exactly what Git
  reports; a file the hook does write is invisible to both by construction,
  which is what keeps the decision identical with logging enabled or disabled.
  - Both questions are properties of the path, not of a name pattern, and both
    answers come from Git's own location and ignore rules. The file need not
    exist: a path that would become a new untracked entry is reported as
    visible before it is created.
  - `WritePolicy` caches the visibility answer per path, because the policy is
    consulted again under the exclusive lock and a second Git call there would
    race with another writer. A run that crosses midnight writes a second
    daily file, so the cache is keyed by path rather than held as one answer.
  - `_query` converts any exception from either Git query to `None`, which
    refuses the write. An unexpected failure therefore cannot make the log
    visible.
  - The constructor defaults for `is_tracked` and `is_visible` in
    `JsonlRecordSink`, `WritePolicy`, and `DecisionLogDeps` are the real Git
    functions, so omitting a collaborator cannot produce a permissive sink.
    The injection points exist for tests; they do not widen the default.
  The gate is asked before anything is created, so a state directory that does
  not exist yet is judged by where Git would report it. The path listings the
  repository queries depend on pass `-z` and split on NUL without stripping
  entries, and `git.run` decodes captured bytes with `os.fsdecode` rather than
  subprocess text mode, because universal-newline translation would turn a
  carriage return in a path into a newline. Git then emits raw, unquoted path
  bytes, so a name containing a quote, a backslash, a newline, a carriage
  return, or non-ASCII bytes still compares as the entry Git reported.
  `core.quotePath=false` is not enough: it affects only bytes above 0x80.
- A daily file the hook does not own must not be written at all. The `start`
  record is written before the uncommitted-changes gate runs, so appending to a
  tracked file would make a clean worktree dirty and could block a stop that
  passes with logging disabled. `JsonlRecordSink` asks `git.is_tracked` before
  its first append to each daily path. `is_tracked` returns `True`, `False`, or
  `None` for "Git could not answer"; only a confirmed `False` permits a write.
  A tracked path, an unknown answer, or an existing file that holds anything
  other than hook records refuses the whole run: the sink warns once with a
  fixed, path-free message, writes no further records, and skips retention.
  Every line of an existing file must carry the producer marker, a known record
  type, and the common fields, and the file must end with a newline. Checking
  only the first line would let a file that begins with a copied or stray hook
  record and continues with unrelated data pass, and appending would then mix
  two writers into one line stream. For the same reason the read is streamed:
  verification holds one partial line at a time and never the whole file, so a
  large genuine daily log is still recognized while peak memory stays bounded.
  A single line longer than the record bound is treated as foreign content and
  preserved untouched. The writer enforces the same bound before appending:
  `JsonlRecordSink.write` drops a record whose encoded line would leave the
  margin below the reader's limit, counts `RecordTooLarge`, and warns with a
  fixed message, while `_bounded_value` truncates each correlation string to
  `MAX_CORRELATION_CHARS`, so an outsized payload identifier cannot grow every
  later record past the limit. Append and retention share the record validator
  in `decision_log/ownership.py`, but they are not eligible for the same set of
  files. The sink accepts the path the clock names as today's daily file when
  this run created it, or when every line verifies. Retention instead requires
  a canonical daily name older than the cutoff, at least one complete record,
  and a file Git confirms untracked that lies outside every worktree or is one
  Git confirms it ignores. Sharing the validator makes foreign content foreign
  to both paths; it does not make every file one path accepts a file the other
  acts on. Pre-existing tracked changes remain visible to the gate because the
  hook never touches the file. The ownership check runs under an exclusive
  `flock` on the daily file, so a concurrent run cannot observe a file that
  another run has created but not yet written, or a half-written line. A
  pre-existing empty file is refused: a run killed between creating the file
  and writing its first record leaves one, but so does a file someone else
  created at the same path, and an empty file proves nothing about who wrote
  it. The cost is bounded: that day's later runs warn and write no records
  until the file is removed. The location has already been approved by then, so
  an empty file left at it is invisible to the gates; refusing it protects the
  record stream, not the decision. A file this run has just created is
  different, because its origin is known, and a run that creates the file and
  then loses the lock race leaves an empty file at an approved location, which
  no gate reports. Ownership is a convention, not proof: a file forged in the
  log directory with the daily name and hook-shaped records is
  indistinguishable from one the hook wrote. The lock is taken with `LOCK_NB`
  and a short bounded retry, and an in-process `threading.Lock` covers threads.
  Both give up rather than wait: another process could hold the file
  indefinitely, and the hook must never stall on its log. A catchable signal
  runs its Python handler on the main thread, so the adapter recognizes a
  same-thread re-entry from the interrupted append. Once the file has been
  approved for the outer write, the handler writes the finish record through
  the active descriptor: the record stays under the same `flock` and needs no
  second lock. Before approval there is no descriptor it may use, so the record
  is refused at once with a warning instead of waiting out a lock the
  interrupted frame cannot release. `git.is_tracked` walks the resolved
  ancestors as well as the lexical ones: an outside symlink into a worktree
  subdirectory has no `.git` above its lexical path, yet Git answers from
  inside the worktree, and reporting it untracked would let the sink append to
  a committed file. `_has_git_ancestor` returns `None` when that resolution
  fails, and every query built on it propagates the unknown answer: the
  resolved ancestors Git itself uses were never inspected, so `False` would
  wrongly mean "outside every worktree" and permit a write or a deletion.
- A gate outcome must distinguish a determined result from an undetermined
  one. The PR rebase gate records `pass` only when the base is known not to be
  ahead; a failed branch, URL, pull-request, fetch, merge-base, or ancestor
  lookup records `skip` with its reason.
- Append each record with one `os.write` on a descriptor opened with
  `O_APPEND`. Do not buffer records or split one record across writes:
  concurrent hooks share the same daily file. Create the file with mode `0600`
  and tighten the open descriptor with `fchmod`, so an existing log written by
  an older version or another process is treated as the owner's rather than
  trusted. `decision_log/platform_support.py` holds the only platform
  differences: POSIX locks with `fcntl.flock`, Windows with `msvcrt.locking`,
  and Windows has no descriptor-level mode call, so it inherits the state
  directory's access control instead.
- One lock wait per run is enough. Once a record has spent the whole lock
  timeout without getting the file, later records make a single non-blocking
  attempt and give up, so a file held by another process cannot multiply the
  delay across a run's remaining records. Each record still warns when its
  write is dropped.
- A configuration opt-out discovered after startup calls `DecisionLog.disable`,
  which writes one terminal `finish` record with `decision = "disabled"` and
  `exit_code = 0` before suppressing every later record. The record marks the
  opt-out, not the hook's eventual decision; its presence keeps a missing
  finish meaning "interrupted".
- A `progress` record with `event = "storage"` carries the run's bounded
  storage telemetry, written immediately before the finish record unless
  nothing attempted storage. Its `operations` list holds one entry each for
  `write` and `prune`; each entry has a fixed `operation` name, attempt and
  failure counts, accumulated storage and lock-wait milliseconds, and a sorted
  list of error categories drawn from a closed set. Never put exception text, a
  path, or command output there. Telemetry is a separate record rather than a
  field of the finish record so the finish record stays about the decision, and
  a reader of storage behaviour does not depend on which terminal record
  happens to include it. It is written first, so it describes earlier records
  and not the finish write itself.
- The log must never change the decision, exit code, or standard output. The
  location gate above is how that is achieved rather than an exception to it:
  the hook writes only where neither repository query can report the file, so
  an enabled run and a disabled run see the same worktree. When the location is
  refused, the run behaves as if logging were disabled, with one fixed warning
  on standard error. A failed write itself never changes the decision, as
  `#203` requires: it warns on standard error and is otherwise ignored, and
  stdout stays reserved for the hook payload. Every warning goes through
  `diagnostics.warn`, which returns without writing when `sys.stderr` is `None`
  (``print(file=None)`` would write to standard output) and swallows `OSError`
  or `ValueError` from a closed or broken stream.
- Log only the documented fields. Never pass command output, arbitrary
  environment values, or unlisted hook payload fields to the log, and bound
  free-text reasons with `_bounded_reason` and payload-derived correlation
  strings with `_bounded_value`. Record a build executable by its driver name,
  not by the path that `POST_TURN_MAKE_BIN` or `POST_TURN_NETSUKE_BIN` may
  supply. The documented `base_ref` field is a hook setting rather than an
  arbitrary environment value.
- A reader that finds a `start` record without a `finish` record must not
  treat the run as a pass. Interruption is the usual cause. A failed write can
  produce the same shape, so check standard error first; a late configuration
  opt-out writes a `disabled` finish and is therefore distinguishable.

`hook.main` resolves settings with `settings_from_env`, constructs the log
before standard input is read, installs a `SignalGuard` for `SIGTERM`,
`SIGINT`, and `SIGHUP` around the remaining work, and finishes in a `finally`
block. That block inspects `sys.exc_info()` so an unexpected exception is
recorded with decision `error` before it propagates. Only the exception's class
name is logged: its message can carry an environment-derived path or command
output. An early configuration block likewise logs a fixed reason while
standard output keeps the detail.

Keep the three responsibilities separate. `DecisionLog` only collects records
and owns the `start`/`finish` lifecycle. `JsonlRecordSink` owns the filesystem:
it resolves the daily path, checks Git ownership, appends one record per
`os.write`, and prunes expired files. `SignalGuard` owns handler installation
and restoration, and calls back into `DecisionLog.record_signal`. `Clock` and
`run_id_factory` are injected so tests can pin timestamps, durations, and
identifiers without patching module globals. `HookState` and the pipeline
depend on the `DecisionRecorder` protocol, never on the concrete adapter, so
gate policy cannot reach the filesystem or the signal machinery.

The recorder does not filter repository results. `hook.main` still passes the
`DecisionLog` to `run_stop_checks` as the `InvocationLog` recorder, but the
pipeline calls `changed_files` and `has_uncommitted_changes` unchanged and
hands their results to the gates as they are. The parity guarantee belongs to
the sink's location gate: a daily file exists only where those queries cannot
report it. That keeps the two halves independent. `git.py` owns what Git
answers, and the decision log owns whether it may write at all; neither needs
to know about the other's special case. The sink's warnings are written
straight to `sys.stderr` rather than through `logging`: an embedding process is
free to route logging to a file, and the hook's promise is a standard-error
warning. A closed or broken standard error is ignored, because reporting a
refusal must never change the decision.

The log is created before any configuration is read, using only
`settings_from_env`, so a stalled or failed configuration read still leaves a
start record and an installed signal handler. Once `load_runtime_config`
returns, `main` resolves the settings again, calls
`DecisionLog.apply_retention` with the now-known window, or
`DecisionLog.disable` when the configuration or environment opted out.
Retention therefore runs before the quality checks but after the configuration
is authoritative, so a window larger than the environment default never deletes
files it meant to keep. The start and input-stage records from an opted-out run
may remain, because they are written before the opt-out is known. An invalid
command line takes a separate early path that writes the block without reading
stdin or running Git.

Construction and pruning are equally defensive. `main` catches a failure to
construct the log, warns on standard error, and continues without one;
`apply_retention` swallows a pruning failure after a fixed warning;
`prune_old_logs` treats a retention window outside the calendar range as "keep
everything", removes a file only when every line carries the hook's producer
marker, a known record type, and the common fields, the file ends with a
newline and is no larger than the verification limit, Git confirms it is
untracked, and either `git.is_in_worktree` confirms it lies outside every
worktree or `git.is_ignored` confirms Git ignores it. The worktree rule is a
decision-parity requirement, not tidiness: deleting an untracked file inside a
worktree changes what the uncommitted-changes gate reports, so an enabled run
could allow where a logging-disabled run blocks. An ignored file is the one
bounded exception, because ignored paths appear in neither repository query:
removing one leaves both runs reporting the same visible state and stops a
state directory inside a repository from retaining every file forever.

Removal is by directory entry and `unlink` has no compare-and-delete form, so
the identity check narrows the replacement window rather than closing it: a
name swapped in the instant between the final check and the unlink is outside
the guarantee. Eligibility is decided from the pathname while the unlink runs
relative to the opened directory, so retention first binds the two: the
directory descriptor's identity must match the path's parent before the checks
and again immediately before the unlink. A renamed or replaced parent is
detected and the entry is preserved, so the file that was checked is the file
that is removed. The sweep only touches the hook's own owner-only files in its
state directory, and another hook never renames entries there, so supported
concurrent use cannot reach that interval. Retention refusing to delete a file
changes nothing the gates can see, because the log directory was approved
before anything was written there. `is_in_worktree`, `is_ignored` and
`is_tracked` each return `True`, `False`, or `None`, and only the exact answer
that makes deletion invisible permits it. Pruning reports scan or delete
failures with fixed, path-free messages; and `SignalGuard` leaves a signal that
was already set to `SIG_IGN` alone, so the hook never overrides a parent's
deliberate disposition. A record whose command or path value contains bytes
that cannot be encoded as strict UTF-8 is serialized with ASCII escaping
instead: a lone surrogate becomes a `\udcXX` escape, the line stays strict
UTF-8, and the record survives. The size bound measures the same escaped line
the writer persists. On `SIGINT` the handler restores Python's default
interrupt handler, so `subprocess.run` can terminate and reap an in-flight
build child instead of orphaning it; other signals keep forcing the default
disposition.

The write path is split along the questions it answers. `WritePolicy` decides
whether a daily file may be used at all: it refuses a location no Git query
proves invisible, a file Git tracks, a file whose tracking status is unknown, a
pre-existing file that is not verifiably the hook's, and any file once it has
refused one. `LockedJsonlAppender` owns everything that touches the file —
creation, the in-process lock and its re-entry guard, the process lock with its
bounded retry, the permission narrowing, and the single `O_APPEND` write with
its truncating rollback. `JsonlRecordSink` composes the two, chooses the daily
path, and counts telemetry into a `StorageStats` collector. Keeping those apart
lets each be tested and replaced without the others; the policy is still
consulted by the adapter while the exclusive lock is held, so an ownership
answer cannot race with another writer. `DecisionLog` owns the run's record
sequence for the same reason: stage and gate state decides what the finish
record contains, so building that record anywhere else would duplicate the
state it reports.

Append is not all-or-nothing, and both failure outcomes are deliberate. A lock
timeout writes no record; a file this run created is left empty at an approved
location, which no gate reports, so the failed write cannot make the worktree
dirty. A short write is truncated back to the length measured under the lock,
so the file keeps only complete records; if that rollback itself fails, the
adapter stops appending for the rest of the run and says so rather than add to
a stream it cannot verify. A record lost to either outcome is reported on
standard error with a fixed, path-free message, and no failure changes the
decision.

Creation cannot be atomic with locking, so a run that creates the daily file
exposes it empty for the few instructions before it takes the lock and writes.
A concurrent run can therefore hold the lock on a zero-length file the hook
itself just made. The adapter rechecks such a file a bounded number of times,
releasing the lock between attempts so its creator can proceed, before the
policy is allowed to call it foreign. A file that stays empty through those
rechecks is still refused exactly as before, so a user's empty file at the
daily path is never claimed; only the creation window is absorbed.

Gate recording already lives in one place. Every gate outcome in `pipeline.py`
is recorded through `_record_gate`, which tolerates a missing log, or through
`_record_gate_decision`, so the gate functions hold policy while the two
helpers hold the recording contract. Returning gate results to an orchestrator
would separate a policy branch from the outcome it decides without removing a
call. The collected outcomes are drained once by `run_stop_checks` in a
`finally` block, and once more immediately before `pr_rebase_check`: that gate
can block in a network fetch, and a signal arriving during the fetch writes the
finish record from the handler before the `finally` block can run. Draining
first keeps passed gates from being reported as never reached. Build-target
groups are published the same way: `_run_build_group` republishes the cumulative
`targets_run` after each group, so a signal during a later group still records
the targets that already finished.

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

The end-to-end hook suite is split by behaviour:
`tests/test_hook_end_to_end.py` covers lifecycle decisions,
`tests/test_hook_log_parity.py` covers location and retention parity with
logging disabled, `tests/test_hook_log_commands.py` covers fetch recording, and
`tests/test_hook_log_interruptions.py` covers signals. Those modules share
repository and process helpers from `tests/hook_support.py`, with the `repo` and
`fake_make` fixtures in `tests/conftest.py`. Decision-log pipeline records are
covered separately in `tests/test_pipeline_logging.py`, whose repeated
preparation patches come from the `prepare_mocks` and `clean_repository`
fixtures.

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
- decision-log settings, retention, record schema, concurrent append safety,
  startup and per-gate timing, the PR-rebase `pass`/`skip` distinction, refusal
  to write inside a worktree that does not ignore the log directory while an
  unrelated untracked file beside an ignored daily file still blocks, including
  one that carries today's daily name, end-to-end allow and block runs that
  compare the decision, standard output, exit code, and persisted finish
  record, runs with a quote, backslash, newline, or carriage return in the
  state path that compare the decision with logging disabled, end-to-end
  `SIGKILL`/ `SIGTERM` behaviour with a real in-flight build command, a
  `SIGTERM` that interrupts a configuration read stalled on a FIFO,
  `tests/test_decision_log_location.py` covering the location gate, and
  Hypothesis properties for location and retention parity,
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

## Running uv through the helper

Every `uv` call in the Makefile goes through `scripts/uv_gate.py`, a copy of
the helper in
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
newer; with an older uv, running the tool once with `uvx` warms the cache.
`uv.lock` changes only through a manual `uv lock`; the gates never change it.

The Makefile exports `NO_COLOR=1` for every recipe. The helper reads the output
of `uv cache dir` verbatim, and a caller that forces colour (`FORCE_COLOR=1`,
as CI log formatters and agent harnesses do) makes uv wrap that path in ANSI
escapes. The helper would then create a cache inside the working directory
under a directory named for the escape sequence. `NO_COLOR` takes precedence
over `FORCE_COLOR`, so the helper always receives a plain cache path. Do not
remove the export or override it in a recipe that calls the helper.
