# Allow the stop when the disk is exhausted

This ExecPlan is a living record. Keep `Progress`, `Surprises & discoveries`,
and `Decision log` current as work proceeds. Issue:
<https://github.com/leynos/post-turn-quality-stop-hook/issues/219>.

## Purpose and outcome

A stop hook exists to make an agent finish its quality work before a turn ends.
When the disk holding the repository or the temporary directory is full, that
purpose inverts: every gate fails for a reason the agent cannot act on, so each
blocked stop re-prompts an agent that can only fail again. On 25 November 2026 a
`/data` volume reaching zero bytes produced roughly thirty such re-prompts in
an hour, and nothing could progress until a person freed space.

After this change, an operator can still rely on every real failure blocking
the stop, while a full disk allows the stop and explains itself once.
Concretely:

- Running the hook on a repository whose temporary volume reports 0 bytes free
  exits `0`, runs no quality target, prints nothing on standard output, and
  prints `quality gates skipped: ...` on standard error.
- A target that fails with `No space left on device` in its output also exits
  `0` with that notice instead of the blocking JSON payload.
- A target that fails for any other reason still prints
  `{"decision": "block", ...}`.
- `POST_TURN_MIN_FREE_MB` changes the free-space floor, and the README documents
  it.

## Context and orientation

The hook is a Python package in `post_turn_quality_stop_hook/`. The relevant
path for this change is short:

- `hook.py` is the console-script entry point. `parse_env` turns environment
  variables into `StopCheckOptions`, and `main` calls
  `pipeline.run_stop_checks`.
- `pipeline.py` orchestrates everything. `prepare_run_stop_checks` resolves the
  repository root, collects Git facts, and computes the changed files.
  `run_quality_checks_if_needed` selects and runs build targets through
  `quality.evaluate_changes`, and `run_stop_checks` emits the decision.
- `quality_policy.py` holds `CommandResult`; `execution.py` is the adapter that
  runs a target group and builds a `CommandResult`; `state.py` holds
  `QualityEvaluation`, `PreparationStop`, and `StopCheckOptions`.
- `formatting.py` renders the blocking reason and emits the JSON payload.
- `docs/users-guide.md`, `docs/developers-guide.md`, and `README.md` document
  runtime behaviour and architecture.

Before the change, `shutil.disk_usage` appeared nowhere in the package and no
test stubbed it, so the suite's behaviour did not depend on the host's free
space. The `tests/conftest.py` file held only a Hypothesis mutation profile and
a workflow-document fixture.

## Milestones

Each milestone leaves the repository in a coherent, validated state.

1. **M1 — policy module and settings.** Add `disk_space.py` with the threshold,
   the temporary-directory rule, the probe, the exhaustion pattern, and byte
   formatting. Default `min_free_mb` and `temp_dir` on `StopCheckOptions`;
   parse both in `parse_env`. No pipeline behaviour changes yet.
2. **M2 — early free-space stop.** Probe the repository and temporary volumes
   immediately after repository-root resolution. Return `PreparationStop` with
   a notice, which `run_stop_checks` writes to standard error before returning
   `0`. A probe failure is logged and ignored.
3. **M3 — indeterminate quality results.** Classify disk exhaustion in
   `run_build_targets` from the untruncated streams. Make
   `QualityEvaluation.indeterminate` true only when every failure reports it,
   and stop blocking in that case.
4. **M4 — tests and documentation.** Unit, in-process and child-process tests,
   plus the README, users' guide, and developers' guide.

## Constraints

- The stdout contract does not change: blocking JSON is the only thing the hook
  ever writes to standard output. Notices go to standard error.
- Only disk exhaustion short-circuits. Any other failure still blocks.
- A volume that cannot be probed is unknown, never low; the hook logs and
  continues.
- The hook must not raise out of a normal path because a directory is missing.
- `ruff` line length is 88; `mccabe` complexity is 8; `pylint` max-args is 4.

## Tolerances (exception triggers)

- Code budget: 700 net lines across production and test files, excluding this
  ExecPlan. Exceeding it is a signal to re-examine scope rather than to raise
  the limit.
- Documentation budget: 400 net lines excluding this ExecPlan.
- Configuration budget: 150 net lines.
- Any new runtime dependency needs approval before it is added.

## Risks

- **Masking a real failure.** A gate could fail for a genuine reason while also
  mentioning ENOSPC in unrelated output. Mitigation: classify only a non-zero
  exit whose output matches, and make indeterminacy require *every* failure to
  match, so one real failure still blocks.
- **A test depending on the host disk.** The probe reads real free space.
  Mitigation: an autouse `tests/conftest.py` fixture stubs
  `disk_space.shutil.disk_usage` with an ample volume; low-space tests override
  it.
- **Noise on a healthy machine.** A threshold that is too high would skip gates
  routinely. Mitigation: a conservative 100 MiB default and a documented `0`
  opt-out.
- **Silent allow.** An operator may wonder why gates did not run. Mitigation:
  the notice names the volume and its measured free space, or the affected
  target groups and repository.

## Verification plan

Obligations, and the evidence that discharges each:

- The temporary directory resolves `CLAUDE_CODE_TMPDIR`, then `TMPDIR`, then
  the platform default, ignoring blank values.
  `tests/test_disk_space.py::TestResolveTempDir`.
- The floor is strict: exactly the threshold is not low; one byte below is.
  `TestCheckFreeSpace::test_free_space_equal_to_the_threshold_is_not_low` and
  `test_free_space_below_the_threshold_is_low`.
- A probe failure is returned, not raised, and does not stop the run.
  `TestCheckFreeSpace::test_probe_errors_are_returned_separately` and
  `tests/test_quality_stage.py::TestPrepareRunStopChecks::test_probe_failure_...`.
- The pattern matches the three documented spellings and rejects near-misses
  such as `MYENOSPC`. `tests/test_disk_space.py` matcher cases.
- Classification reads pre-truncation, pre-strip output: a marker in the middle
  of a 40 000-character stream and a colour-wrapped marker both classify.
  `tests/test_execution.py::TestDiskExhaustionClassification`.
- `disk_exhausted` is false for a zero exit, the empty-target sentinel, and a
  missing executable. Same class.
- Indeterminacy requires every failure to match, and a genuine failure beside an
  exhausted one still blocks.
  `tests/test_quality_stage.py::TestQualityEvaluationDiskExhaustion` and a
  Hypothesis property, `TestQualityEvaluationProperties`.
- The preparation short-circuit runs before Git facts, writes a notice, and
  keeps stdout empty.
  `TestPrepareRunStopChecks::test_low_temporary_space_stops_...`,
  `test_low_repository_space_stops_...`, and
  `test_run_stop_checks_prints_the_notice_and_allows_the_stop`.
- A quiet non-repository stop still writes no notice.
  `test_quiet_stop_writes_no_notice`.
- The indeterminate quality result is terminal: notice on stderr, `0`, stdout
  empty, branch gates not run. `TestRunStopChecksDiskExhaustion`.
- End to end through child processes: an ENOSPC target allows the stop with a
  notice, and a low floor skips the targets.
  `tests/test_hook_cli_integration.py`.
- Existing behaviour is unchanged for other failures:
  `test_enabled_quality_gate_blocks_on_a_failing_target` still passes.

Non-vacuity: the low-space tests override the ample fixture with a concrete
free value and assert the exact notice text, so a probe that never ran, or a
classifier that always returns true, fails them. The Hypothesis property
generates exit codes and both classification flags, so an implementation that
ignored either input is rejected. The truncation test asserts the marker is
absent from the captured stream *and* that classification still fired, so a
classifier that read the truncated text fails.

External axiom: `shutil.disk_usage` reports free bytes for an existing path and
raises `OSError` otherwise. Both branches are exercised; the library's internal
correctness is not verified here.

## Progress

- [x] 2026-11-28 M1: `disk_space.py` written; `StopCheckOptions` and `parse_env`
      carry `min_free_mb` and `temp_dir`.
- [x] 2026-11-28 M2: preparation probe with a `PreparationStop` notice;
      `run_stop_checks` writes notices to stderr and returns `0`.
- [x] 2026-11-28 M3: `CommandResult.disk_exhausted`,
      `QualityEvaluation.failures`
      and `.indeterminate`, `QualityStop`, and the terminal allow.
- [x] 2026-11-28 M4: tests across five files and a shared autouse fixture;
      README, users' guide, and developers' guide updated.
- [ ] Full gate set and CodeRabbit review.

## Surprises & discoveries

- The worktree's `.venv` had been created with a free-threaded Python 3.15 that
  cannot build `cffi` for `github3.py` because the host has no libffi headers.
  Recreating it with Python 3.14.7 satisfied `uv sync --locked`, so this was an
  environment repair rather than a dependency change.
- pytest's bundled ruff rules (`PT011`, the "patch with a lambda" rule) apply to
  tests: a stubbed `shutil.disk_usage` has to be a named function or
  `return_value=`, and a literal `/tmp` in test data trips the hardcoded-temp
  rule, so the tests use neutral paths such as `/scratch`.
- `QualityEvaluation.blocked` had to stay the single blocking predicate rather
  than gaining a parallel "allow" flag, so `run_quality_checks_if_needed` could
  keep returning `None` for "nothing to report" while a new `QualityStop`
  carries the notice.

## Decision log

- **Two independent checks, not one.** A pre-check cannot see a disk that fills
  during the run, and output classification cannot report why the gates never
  started. Both are kept; they share the pattern and the formatter.
- **Notice on standard error.** Claude Code reads the decision from standard
  output. A notice there could be parsed as a malformed decision, so notices
  use standard error, which also matches the existing `logger.warning`
  diagnostics.
- **`min_free_mb: int | None = None` on the preparation function.** Direct
  callers (and the existing tests) keep their signature; only `run_stop_checks`
  passes the configured value. `None` and `0` both skip the probe.
- **Classify from raw output.** Truncation keeps the head and tail and ANSI
  stripping can split the evidence, so classification runs on the raw streams
  before either transformation. The flag is `False` for a zero exit so a
  successful command that mentions ENOSPC in a log line cannot allow a stop.
- **`QualityStop` as a distinct outcome.** Returning a sentinel `HookState`
  would have made "blocked" ambiguous with "nothing to report"; a separate type
  keeps the orchestrator's match exhaustive.
- **Skip branch gates on an indeterminate quality result.** They could block on
  the same full disk, which is exactly the loop this change removes.

## Outcomes & retrospective

The hook now distinguishes "the change is wrong" from "the machine cannot
answer". Quality failures keep their authority; disk exhaustion allows the stop
once, with a message a person can act on.

Lessons worth keeping:

- A test suite that reads ambient machine state silently becomes
  environment-dependent; the autouse fixture is now the guard.
- The `No space left on device` family is not one string. Matching all three
  documented spellings, word-bounded, was necessary for the child-process test
  that prints the GNU spelling while Rust tools print `os error 28`.
