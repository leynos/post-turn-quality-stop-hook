# Architectural decision record (ADR) 004: Repository-operations port

## Status

Accepted on 2026-11-26. Preparation and the uncommitted-changes gate ask a
root-bound `RepositoryOperations` port domain questions about the repository,
and the Git adapter that answers them is bound only at the composition root.

## Date

2026-11-26.

## Context and problem statement

The decision-log feature writes a daily JSON Lines file outside every worktree,
or where Git ignores it ([ADR-003](adr-003-per-invocation-decision-log.md)). An
earlier revision threaded the log directory through the pipeline as an
exclusion path: the Git adapter accepted `changed_files(..., exclude=...)` and
`has_uncommitted_changes(..., exclude=...)`, and `InvocationLog.exclude_paths`
carried the value. That kept the storage detail out of the gate *functions*,
but the pipeline still knew that a *path* was being excluded. Gate policy was
one refactor away from learning where the hook stores its own records, and a
test that exercised a gate had to know the same path.

A review of pull request #204 raised this as a domain-architecture problem, and
issue #207 tracks the follow-up. By the time the follow-up started, the log's
location rules had made the exclusion unnecessary: a location a worktree could
report is refused for the whole run, so a live log can never appear as changed
work. The adapter-level parameters were gone, but two problems remained:

- Preparation called `changed_files(repo, base_commit)` directly, importing a
  Git helper into the orchestrator.
- The uncommitted-changes gate asked the `BranchState` port for dirtiness and
  for the current branch, passing a repository path to both questions. A gate
  therefore still received a path, and the port mixed branch-state questions
  with working-tree questions.

## Decision drivers

- Gate policy must ask domain questions about a repository, not orchestrate
  Git commands or receive paths it does not need.
- A storage location must not be expressible in a gate's signature, even as an
  optional exclusion.
- A failed query must stay distinguishable from a negative answer: an
  unreadable tree is not a clean tree.
- Tests must decide gate policy from prepared answers without patching Git
  imports or creating a worktree.
- The existing `DecisionRecorder` protocol in `recording.py` remains the
  recording seam; this decision changes the repository boundary only.

## Decision outcome

Add `repository_port.py` and `repository_git.py`:

- `RepositoryOperations` is a structural protocol with three questions:
  `changed_paths_since(base_commit)`, `has_uncommitted_work()`, and
  `current_branch()`. Each returns the adapter's `(value, HookError | None)`
  shape. Changed paths are sorted and deduplicated and cover staged, unstaged,
  and non-ignored untracked files. `has_uncommitted_work` answers `False` only
  when the question was answered and the tree is clean; `None` with an error
  means it could not be answered.
- Implementations bind the repository root when they are constructed. The
  methods take no repository path, no flag set, and no path to exclude.
- `RepositoryOperationsFactory` builds the port for a resolved root.
  `run_stop_checks` forwards the factory to preparation and uses it again for
  the gate phase; `hook.main` supplies `GitRepositoryOperations`, the adapter
  that delegates to `git.py`.
- `uncommitted_changes_gate(repository, options)` takes the port and no
  repository path. It preserves the precedence it had: a disabled gate returns
  first; the branch read decides a protected-branch skip or a Git error before
  any dirtiness query; an unreadable dirty answer passes with its error; only a
  definite dirty answer blocks.
- `BranchState` keeps the questions the unpushed preflight and the PR rebase
  check still ask. `has_uncommitted_changes` moves off it, so one port owns
  each question.

## Alternatives considered

- **Extend `BranchState` with `changed_paths_since`.** Rejected: every
  branch-state question takes a repository path, so the uncommitted gate would
  keep receiving one, and preparation would import a branch-state port it does
  not otherwise need. The two ports also serve different consumers: the
  branch-state gates decide transport-heavy questions, while the repository
  operations answer working-tree questions for preparation and one gate.
- **Keep the exclusion parameters and make them domain-shaped.** Rejected: an
  `exclude` parameter is still a storage path in the decision path. The log's
  refusal rule already answers the underlying problem, so the parameter is
  unnecessary.
- **Build the Git adapter inside `pipeline.py`.** Rejected: the orchestrator
  would import the adapter and tests would patch module attributes again. The
  factory keeps the adapter at the composition root.
- **Return a list from `changed_paths_since`.** Rejected: the result feeds an
  immutable reporting snapshot and a tuple states that the caller cannot mutate
  it.
- **Treat an unreadable tree as clean to keep passing.** Rejected: it would
  hide a Git failure behind an allow decision. The gate keeps its existing
  fail-open-with-error policy, which records the error rather than claiming a
  pass with no evidence.

## Consequences

- Gate policy and preparation import no Git module. The fresh-interpreter and
  source-level boundary tests cover the new port as well as the gate module.
- A test supplies `FakeRepositoryOperations` and records every question asked,
  so it can prove that a protected branch is never asked about dirtiness, that
  a disabled gate asks nothing, and that no repository question runs after a
  quality block.
- `current_branch` is declared by both ports. That is deliberate: the
  branch-state port answers it as one of several per-call questions, while the
  repository port answers it for the uncommitted gate without a path. Each port
  is bound for its own consumer rather than sharing one shape.
- A future adapter may omit paths it owns without the port changing and
  without a gate learning the path. The omission belongs to the adapter because
  only the adapter knows the repository layout.
- `repository_git.py` is the only new place that reaches `git.py` for these
  questions, so the adapter is the single site to change if the underlying Git
  invocation changes.
