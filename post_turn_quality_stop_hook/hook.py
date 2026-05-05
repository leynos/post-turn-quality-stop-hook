#!/usr/bin/env python3
"""Claude Code Stop-hook quality gate.

At "turn end" (Claude Code Stop hook):
1) Ensure refs/remotes/origin/main exists (git fetch only if missing by default)
2) Compute changed files vs origin/main using merge-base(origin/main, HEAD)
3) If changes exist:
   - Python/TypeScript files run `make check-fmt lint typecheck`.
   - Rust files run `make check-fmt lint`.
   - Markdown files run `make markdownlint`.
   Only targets present in the Makefile are run.
4) If any invoked command fails, BLOCK the stop with a detailed reason.

Behaviour knobs (env vars):
- POST_TURN_ALWAYS_FETCH=1 always fetches `origin/main`.
- POST_TURN_BASE_REF=... overrides the base ref.
- POST_TURN_MAX_OUTPUT_CHARS truncates per-command output.
- POST_TURN_COMPUSH=1 blocks if local work remains unpublished.

Claude Code contract:
- Reads JSON hook input from stdin (but works even if stdin isn't JSON)
- On failure: prints JSON {"decision":"block","reason":"..."} to stdout and exits 0
- On success: prints nothing and exits 0

Examples
--------
Run the hook manually with a default environment:

    POST_TURN_ALWAYS_FETCH=1 post-turn-quality-stop-hook < /dev/null

"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess  # noqa: S404
import sys
import typing as typ
from pathlib import Path

PY_TS_EXTS = {".py", ".pyi", ".ts", ".tsx", ".mts", ".cts"}
RUST_EXTS = {".rs"}
MD_EXTS = {".md", ".mdx", ".markdown"}

CATS_TO_TARGETS: dict[str, list[str]] = {
    "python_ts": ["check-fmt", "lint", "typecheck"],
    "rust": ["check-fmt", "lint"],
    "markdown": ["markdownlint"],
}
CODE_CATS = {"python_ts", "rust"}
MD_CATS = {"markdown"}
TRUTHY_VALUES = {"1", "true", "yes"}
MAX_CHANGED_FILES_IN_REASON = 60
MAKE_FAILURE_EXIT = 2
BLOCKED_STATUS = 1
SUPPORTED_BUILD_DRIVERS = {"auto", "netsuke", "make"}
MAKE_TARGET_PROBE = "__post_turn_quality_stop_hook_target_probe__"


class CommandResult(typ.TypedDict):
    """Result of a build-target invocation.

    Attributes
    ----------
    kind
        Label describing the target group (``"code"`` or ``"markdown"``).
    cmd
        Full command that was executed.
    exit_code
        Process exit code.
    stdout
        Captured standard output (may be truncated).
    stderr
        Captured standard error.

    """

    kind: str
    cmd: str
    exit_code: int
    stdout: str
    stderr: str


def default_categories() -> dict[str, bool]:
    """Return a default category mapping.

    Returns
    -------
    dict[str, bool]
        Default mapping of category names to enabled flags.

    """
    return {"python_ts": False, "rust": False, "markdown": False}


@dataclasses.dataclass
class HookState:
    """Execution state for the stop hook.

    Attributes
    ----------
    ok
        Whether the hook checks succeeded.
    base_ref
        Base ref used for comparison.
    base_commit
        Resolved merge-base commit.
    changed_files
        Files changed relative to the base commit.
    categories
        Detected change categories.
    build_driver
        Build driver selected to run quality targets.
    targets_requested
        Build targets requested based on change categories.
    targets_run
        Build targets executed.
    targets_skipped
        Requested targets that were not present for the selected driver.
    commands
        Executed commands and their outputs.
    fetched
        Whether a fetch was performed.
    error
        Error message when blocking.

    """

    ok: bool = True
    base_ref: str = "origin/main"
    base_commit: str | None = None
    changed_files: list[str] = dataclasses.field(default_factory=list)
    categories: dict[str, bool] = dataclasses.field(default_factory=default_categories)
    build_driver: str | None = None
    targets_requested: list[str] = dataclasses.field(default_factory=list)
    targets_run: list[str] = dataclasses.field(default_factory=list)
    targets_skipped: list[str] = dataclasses.field(default_factory=list)
    commands: list[CommandResult] = dataclasses.field(default_factory=list)
    fetched: bool = False
    error: str | None = None


@dataclasses.dataclass
class RunStopChecksPreparation:
    """Prepared state for ``run_stop_checks``.

    Attributes
    ----------
    ok
        Whether preparation succeeded and execution should continue.
    exit_code
        Exit code to return immediately when preparation did not succeed.
    state
        Hook state populated during preparation.
    repo
        Resolved repository root when preparation succeeded.

    """

    ok: bool
    exit_code: int
    state: HookState
    repo: Path | None = None


@dataclasses.dataclass(frozen=True)
class StopCheckOptions:
    """Runtime options for stop-hook checks.

    Attributes
    ----------
    always_fetch
        Whether to always fetch origin/main.
    max_out
        Maximum number of output characters to capture.
    compush
        Whether to remind the agent to commit and push when dirty.
    build_driver
        Requested build driver: ``auto``, ``netsuke``, or ``make``.
    netsuke_bin
        Executable used when running Netsuke.
    make_bin
        Executable used when running Make.

    """

    always_fetch: bool
    max_out: int
    compush: bool = False
    build_driver: str = "auto"
    netsuke_bin: str = "netsuke"
    make_bin: str = "make"


@dataclasses.dataclass(frozen=True)
class BuildDriver:
    """Quality-gate build driver.

    Attributes
    ----------
    name
        Human-readable driver name.
    executable
        Executable path or command name.
    manifest
        Repository manifest file that identifies the driver.

    """

    name: str
    executable: str
    manifest: str


@dataclasses.dataclass(frozen=True)
class BuildTargetRequest:
    """A grouped build-target invocation.

    Attributes
    ----------
    driver
        Build driver used to run the targets.
    kind
        Label describing the target group.
    targets
        Build targets to run.

    """

    driver: BuildDriver
    kind: str
    targets: list[str]


@dataclasses.dataclass(frozen=True)
class DriverAvailability:
    """Available build-driver manifests and executables."""

    netsuke: BuildDriver
    make: BuildDriver
    has_netsukefile: bool
    has_makefile: bool
    has_netsuke: bool
    has_make: bool
    has_unusable_netsukefile: bool


def _subprocess_env() -> dict[str, str]:
    """Return an environment with enriched PATH for subprocess execution.

    The hook may run under a restricted PATH (for example, when launched
    as a Claude Code hook).  Prepend standard user-local tool directories
    so that ``make`` and ``netsuke`` can find ``ruff``, ``ty``,
    ``markdownlint-cli2``, and similar quality tools.

    Returns
    -------
    dict[str, str]
        Environment mapping with an extended ``PATH``.

    """
    env = os.environ.copy()
    home = Path.home()
    extra_dirs = [
        home / ".local" / "bin",
        home / ".bun" / "bin",
        home / ".cargo" / "bin",
        home / ".lody" / "bin",
        home / "go" / "bin",
    ]
    existing_path = env.get("PATH", "")
    entries = existing_path.split(os.pathsep)
    for d in extra_dirs:
        d_str = str(d)
        if d.is_dir() and d_str not in entries:
            entries.insert(0, d_str)
    env["PATH"] = os.pathsep.join(entries)
    return env


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command in the given working directory.

    Parameters
    ----------
    cmd
        Command and arguments to run.
    cwd
        Working directory for the subprocess.

    Returns
    -------
    subprocess.CompletedProcess[str]
        Completed process with captured output.

    """
    try:
        return subprocess.run(  # noqa: S603  # valid: command and args are controlled (no shell, no user-supplied command strings).
            cmd,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            env=_subprocess_env(),
        )
    except FileNotFoundError as exc:
        if Path(exc.filename or "") != cwd:
            raise
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr=str(exc)
        )
    except NotADirectoryError as exc:
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr=str(exc)
        )


def truncate(text: str, max_chars: int) -> str:
    """Truncate text to a maximum length.

    Parameters
    ----------
    text
        Text to truncate.
    max_chars
        Maximum number of characters to keep.

    Returns
    -------
    str
        Truncated text with a placeholder if needed.

    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "\n... (output truncated) ...\n"
    if max_chars <= len(marker):
        return text[:max_chars]
    remaining = max_chars - len(marker)
    head = remaining // 2
    tail = remaining - head
    return text[:head] + marker + text[-tail:]


def repo_root(start_cwd: Path) -> tuple[Path | None, str | None]:
    """Resolve the git repository root for a starting directory.

    Parameters
    ----------
    start_cwd
        Directory to resolve from.

    Returns
    -------
    tuple[Path | None, str | None]
        Repository root path and error message, if any.

    """
    p = run(["git", "rev-parse", "--show-toplevel"], start_cwd)
    if p.returncode != 0:
        err = p.stderr.strip() or p.stdout.strip() or "not a git repository"
        return None, err
    root = p.stdout.strip()
    if not root:
        return None, "git rev-parse --show-toplevel returned empty output"
    return Path(root), None


def ensure_origin_remote(repo: Path) -> tuple[bool, str | None]:
    """Ensure the origin remote is configured.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    tuple[bool, str | None]
        ok and error message, if any.

    """
    remotes = run(["git", "remote"], repo)
    if remotes.returncode != 0:
        return (
            False,
            f"git remote failed: {remotes.stderr.strip() or remotes.stdout.strip()}",
        )
    if "origin" not in remotes.stdout.split():
        return False, "git remote 'origin' not found"
    return True, None


def fetch_origin_main(repo: Path) -> tuple[bool, str | None]:
    """Fetch origin/main.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    tuple[bool, str | None]
        ok and error message, if any.

    """
    fetch = run(["git", "fetch", "--quiet", "origin", "main"], repo)
    if fetch.returncode != 0:
        error_output = fetch.stderr.strip() or fetch.stdout.strip()
        return (
            False,
            f"git fetch origin main failed: {error_output}",
        )
    return True, None


def ref_exists(repo: Path, ref: str) -> tuple[bool, str | None]:
    """Check whether a ref exists.

    Parameters
    ----------
    repo
        Repository root path.
    ref
        Fully qualified ref name.

    Returns
    -------
    tuple[bool, str | None]
        True if the ref exists, otherwise False and an error if the check failed.

    """
    verify = run(["git", "show-ref", "--verify", "--quiet", ref], repo)
    if verify.returncode == 0:
        return True, None
    if verify.returncode == 1:
        return False, None
    return (
        False,
        verify.stderr.strip() or verify.stdout.strip() or "git show-ref failed",
    )


def verify_ref(repo: Path, ref: str) -> tuple[bool, str | None]:
    """Verify that a ref can be resolved.

    Parameters
    ----------
    repo
        Repository root path.
    ref
        Ref name to verify with rev-parse.

    Returns
    -------
    tuple[bool, str | None]
        ok and error message, if any.

    """
    rp = run(["git", "rev-parse", "--verify", "--quiet", ref], repo)
    if rp.returncode != 0:
        return False, f"Cannot resolve {ref}"
    return True, None


def ensure_origin_main(
    repo: Path, *, always_fetch: bool
) -> tuple[bool, str | None, bool]:
    """Ensure origin/main is present and resolvable.

    Parameters
    ----------
    repo
        Repository root path.
    always_fetch
        If True, always fetch origin/main.

    Returns
    -------
    tuple[bool, str | None, bool]
        ok, error message (if any), fetched.

    """
    ok, err = ensure_origin_remote(repo)
    if not ok:
        return False, err, False

    ok, err, fetched = ensure_origin_main_ref(repo, always_fetch=always_fetch)
    if not ok:
        return False, err, fetched

    ok, err = verify_ref(repo, "origin/main")
    if not ok:
        return False, err, fetched
    return True, None, fetched


def ensure_origin_main_ref(
    repo: Path, *, always_fetch: bool
) -> tuple[bool, str | None, bool]:
    """Ensure refs/remotes/origin/main exists, fetching if needed.

    Parameters
    ----------
    repo
        Repository root path.
    always_fetch
        If True, always fetch origin/main.

    Returns
    -------
    tuple[bool, str | None, bool]
        ok, error message (if any), fetched.

    """
    fetched = always_fetch
    if not always_fetch:
        exists, err = ref_exists(repo, "refs/remotes/origin/main")
        if err:
            return False, err, fetched
        if exists:
            return True, None, fetched

    ok, err = fetch_origin_main(repo)
    if not ok:
        return False, err, fetched
    fetched = True

    exists, err = ref_exists(repo, "refs/remotes/origin/main")
    if err:
        return False, err, fetched
    if not exists:
        return False, "origin/main still missing after fetch", fetched

    return True, None, fetched


def ensure_base_ref(
    repo: Path,
    base_ref: str,
    *,
    always_fetch: bool,
) -> tuple[bool, str | None, bool]:
    """Ensure a base ref is available and resolvable.

    Parameters
    ----------
    repo
        Repository root path.
    base_ref
        Base git ref used to compute the merge-base.
    always_fetch
        If True, always fetch origin/main when base_ref is origin/main.

    Returns
    -------
    tuple[bool, str | None, bool]
        ok, error message (if any), fetched.

    """
    if base_ref == "origin/main":
        return ensure_origin_main(repo, always_fetch=always_fetch)

    ok, err = verify_ref(repo, base_ref)
    if not ok:
        return False, err or f"Cannot resolve base ref '{base_ref}'", False
    return True, None, False


def merge_base(repo: Path, base_ref: str) -> tuple[str | None, str | None]:
    """Compute the merge-base of base_ref and HEAD.

    Parameters
    ----------
    repo
        Repository root path.
    base_ref
        Base ref to compare against HEAD.

    Returns
    -------
    tuple[str | None, str | None]
        Merge-base commit hash and error message, if any.

    """
    p = run(["git", "merge-base", base_ref, "HEAD"], repo)
    if p.returncode != 0:
        error_output = p.stderr.strip() or p.stdout.strip()
        return (
            None,
            f"git merge-base {base_ref} HEAD failed: {error_output}",
        )
    base = p.stdout.strip()
    if not base:
        return None, "git merge-base returned empty output"
    return base, None


def changed_files(repo: Path, base_commit: str) -> tuple[list[str] | None, str | None]:
    """List files changed relative to a base commit.

    Parameters
    ----------
    repo
        Repository root path.
    base_commit
        Base commit hash for diffing.

    Returns
    -------
    tuple[list[str] | None, str | None]
        Sorted list of changed files and error message, if any.

    """
    changed: set[str] = set()

    # Tracked changes (unstaged and staged) relative to base_commit
    for args in (
        ["git", "diff", "--name-only", base_commit],
        ["git", "diff", "--cached", "--name-only", base_commit],
    ):
        p = run(args, repo)
        if p.returncode != 0:
            return (
                None,
                f"{' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}",
            )
        for line in p.stdout.splitlines():
            line = line.strip()
            if line:
                changed.add(line)

    # Untracked (but not ignored)
    u = run(["git", "ls-files", "--others", "--exclude-standard"], repo)
    if u.returncode != 0:
        return None, f"git ls-files failed: {u.stderr.strip() or u.stdout.strip()}"
    for line in u.stdout.splitlines():
        line = line.strip()
        if line:
            changed.add(line)

    return sorted(changed), None


def has_uncommitted_changes(repo: Path) -> tuple[bool | None, str | None]:
    """Check whether the working tree has uncommitted or untracked changes.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    tuple[bool | None, str | None]
        True if dirty, False if clean, None on error; and an error message.

    """
    for args in (
        ["git", "diff", "--quiet"],
        ["git", "diff", "--cached", "--quiet"],
    ):
        p = run(args, repo)
        if p.returncode == 1:
            return True, None
        if p.returncode != 0:
            return (
                None,
                f"{' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}",
            )

    u = run(["git", "ls-files", "--others", "--exclude-standard"], repo)
    if u.returncode != 0:
        return None, f"git ls-files failed: {u.stderr.strip() or u.stdout.strip()}"
    if u.stdout.strip():
        return True, None

    return False, None


def get_upstream_ref(repo: Path) -> tuple[str | None, str | None]:
    """Get the upstream tracking ref for the current branch.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    tuple[str | None, str | None]
        Upstream ref name (e.g. ``origin/main``) and error message, if any.

    """
    p = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo)
    if p.returncode != 0:
        return None, p.stderr.strip() or p.stdout.strip() or "no upstream configured"
    ref = p.stdout.strip()
    if not ref:
        return None, "git rev-parse returned empty upstream"
    return ref, None


def has_unpushed_commits(repo: Path, upstream: str) -> tuple[bool | None, str | None]:
    """Check whether ``HEAD`` is ahead of the given upstream ref.

    Parameters
    ----------
    repo
        Repository root path.
    upstream
        Upstream tracking ref to compare against.

    Returns
    -------
    tuple[bool | None, str | None]
        True if local commits are ahead, False if not, None on error; and an
        error message.

    """
    p = run(["git", "rev-list", "--count", f"{upstream}..HEAD"], repo)
    if p.returncode != 0:
        return None, (
            f"git rev-list --count {upstream}..HEAD failed: "
            f"{p.stderr.strip() or p.stdout.strip()}"
        )

    ahead = p.stdout.strip()
    if not ahead:
        return None, "git rev-list --count returned empty output"

    try:
        return int(ahead) > 0, None
    except ValueError:
        return None, f"git rev-list --count returned non-integer output: {ahead}"


def detect_categories(files: list[str]) -> dict[str, bool]:
    """Detect change categories from a file list.

    Parameters
    ----------
    files
        List of file paths.

    Returns
    -------
    dict[str, bool]
        Mapping of category names to detection flags.

    """
    cats = default_categories()
    for f in files:
        ext = Path(f).suffix.lower()
        if ext in PY_TS_EXTS:
            cats["python_ts"] = True
        if ext in RUST_EXTS:
            cats["rust"] = True
        if ext in MD_EXTS:
            cats["markdown"] = True
    return cats


def parse_make_targets(make_stdout: str) -> set[str]:
    """Parse make database output for target names.

    Parameters
    ----------
    make_stdout
        Stdout from make -p.

    Returns
    -------
    set[str]
        Parsed Make target names.

    """
    targets: set[str] = set()
    rule_re = re.compile(r"^([^\s:#=]+(?:\s+[^\s:#=]+)*)\s*(?:::|\:(?!\=))\s*.*$")
    for line in make_stdout.splitlines():
        if not line:
            continue
        if line.startswith(("#", "\t", " ")):
            continue
        m = rule_re.match(line)
        if not m:
            continue
        lhs = m.group(1)
        for t in lhs.split():
            if "%" in t:
                continue
            if t != MAKE_TARGET_PROBE:
                targets.add(t)
    return targets


def parse_netsuke_targets(manifest_stdout: str) -> set[str]:
    """Parse generated Ninja build edges from ``netsuke manifest -``.

    Parameters
    ----------
    manifest_stdout
        Generated Ninja manifest printed by Netsuke.

    Returns
    -------
    set[str]
        Parsed explicit build target names.

    """
    targets: set[str] = set()
    for line in manifest_stdout.splitlines():
        if not line.startswith("build "):
            continue
        outputs, _separator, _rule = line.removeprefix("build ").partition(":")
        for output in outputs.split():
            if output.startswith(("$", "|")):
                continue
            targets.add(output)
    return targets


def is_missing_makefile(output: str) -> bool:
    """Check output for a missing Makefile condition.

    Parameters
    ----------
    output
        Combined output from make.

    Returns
    -------
    bool
        True if the output indicates no Makefile was found.

    """
    lowered = output.lower()
    return "no makefile found" in lowered


def get_make_targets(
    repo: Path, executable: str = "make"
) -> tuple[set[str] | None, str | None]:
    """Collect available Make targets from a repository.

    Parameters
    ----------
    repo
        Repository root path.
    executable
        Make executable to run.

    Returns
    -------
    tuple[set[str] | None, str | None]
        Target set and error message, if any.

    """
    try:
        p = run(
            [
                executable,
                "-p",
                "--no-print-directory",
                f"--eval={MAKE_TARGET_PROBE}:",
                MAKE_TARGET_PROBE,
            ],
            repo,
        )
    except FileNotFoundError:
        return None, f"{executable} not found on PATH"

    if p.returncode == MAKE_FAILURE_EXIT:
        combined = f"{p.stderr.strip()}\n{p.stdout.strip()}".strip()
        if is_missing_makefile(combined):
            return set(), None
        return None, combined or "make -p failed"

    return parse_make_targets(p.stdout), None


def get_netsuke_targets(
    repo: Path, executable: str = "netsuke"
) -> tuple[set[str] | None, str | None]:
    """Collect available Netsuke targets from a repository.

    Parameters
    ----------
    repo
        Repository root path.
    executable
        Netsuke executable to run.

    Returns
    -------
    tuple[set[str] | None, str | None]
        Target set and error message, if any.

    """
    try:
        p = run([executable, "manifest", "-"], repo)
    except FileNotFoundError:
        return None, f"{executable} not found on PATH"

    if p.returncode != 0:
        combined = f"{p.stderr.strip()}\n{p.stdout.strip()}".strip()
        return None, combined or f"{executable} manifest - failed"

    return parse_netsuke_targets(p.stdout), None


def get_build_targets(
    repo: Path, driver: BuildDriver
) -> tuple[set[str] | None, str | None]:
    """Collect available build targets for a selected driver.

    Parameters
    ----------
    repo
        Repository root path.
    driver
        Build driver to use for target enumeration.

    Returns
    -------
    tuple[set[str] | None, str | None]
        Target set and error message, if any.

    """
    if driver.name == "netsuke":
        return get_netsuke_targets(repo, driver.executable)
    return get_make_targets(repo, driver.executable)


def dedup_preserve_order(items: list[str]) -> list[str]:
    """Deduplicate items while preserving order.

    Parameters
    ----------
    items
        Items to deduplicate.

    Returns
    -------
    list[str]
        Deduplicated items in original order.

    """
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def run_build_targets(
    repo: Path,
    request: BuildTargetRequest,
    max_out: int,
) -> CommandResult:
    """Run build targets and capture output.

    Parameters
    ----------
    repo
        Repository root path.
    request
        Grouped build-target request.
    max_out
        Maximum number of output characters to capture.

    Returns
    -------
    CommandResult
        TypedDict with keys ``kind``, ``cmd``, ``exit_code``, ``stdout``,
        and ``stderr`` representing execution metadata and captured output.

    """
    if not request.targets:
        return {
            "kind": request.kind,
            "cmd": "",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }

    cmd = build_command(request.driver, request.targets)
    try:
        p = run(cmd, repo)
    except FileNotFoundError as exc:
        return {
            "kind": request.kind,
            "cmd": " ".join(cmd),
            "exit_code": 127,
            "stdout": "",
            "stderr": f"{request.driver.executable} not found on PATH: {exc}",
        }
    return {
        "kind": request.kind,
        "cmd": " ".join(cmd),
        "exit_code": int(p.returncode),
        "stdout": truncate(p.stdout, max_out),
        "stderr": truncate(p.stderr, max_out),
    }


def build_command(driver: BuildDriver, targets: list[str]) -> list[str]:
    """Return the command used to build targets with a driver."""
    if driver.name == "netsuke":
        return [driver.executable, "build", *targets]
    return [driver.executable, "--no-print-directory", *targets]


def _detected_category_labels(categories: dict[str, bool]) -> list[str]:
    """Return human-readable labels for detected change categories."""
    labels: list[str] = []
    if categories.get("python_ts"):
        labels.append("Python/TypeScript")
    if categories.get("rust"):
        labels.append("Rust")
    if categories.get("markdown"):
        labels.append("Markdown")
    return labels


def _format_changed_files(state: HookState) -> list[str]:
    """Format the changed-file section for a blocking reason."""
    base_ref = state.base_ref or "?"
    changed = state.changed_files
    lines = ["", f"Changed files vs {base_ref}: {len(changed)}"]
    lines.extend(f"- {path}" for path in changed[:MAX_CHANGED_FILES_IN_REASON])
    if len(changed) > MAX_CHANGED_FILES_IN_REASON:
        remaining = len(changed) - MAX_CHANGED_FILES_IN_REASON
        lines.append(f"- ... (+{remaining} more)")
    return lines


def _format_target_summary(state: HookState) -> list[str]:
    """Format requested, run, and skipped build targets."""
    lines: list[str] = []
    if state.build_driver:
        lines.extend(("", f"Build driver: {state.build_driver}"))
    if state.targets_requested:
        lines.extend((
            "",
            "Requested build targets: " + " ".join(state.targets_requested),
        ))
    if state.targets_run:
        lines.append("Targets run: " + " ".join(state.targets_run))
    if state.targets_skipped:
        lines.append("Targets skipped (missing): " + " ".join(state.targets_skipped))
    return lines


def _format_command_failure(command: CommandResult) -> list[str]:
    """Format one failed command for the blocking reason."""
    cmd = command.get("cmd", "")
    code = command.get("exit_code", "?")
    combined = "\n".join([
        x for x in [command.get("stdout", ""), command.get("stderr", "")] if x
    ]).strip()
    return [
        "",
        f"Command failed (exit {code}): {cmd}",
        "```",
        combined or "(no output captured)",
        "```",
    ]


def format_reason(state: HookState) -> str:
    """Format a blocking reason for hook output.

    Parameters
    ----------
    state
        Hook execution state.

    Returns
    -------
    str
        Human-readable reason string.

    """
    lines: list[str] = ["Post-turn checks failed."]

    if state.error:
        lines.extend(("", f"Error: {state.error}"))

    base_ref = state.base_ref or "?"
    base_commit = state.base_commit or "?"
    lines.extend(("", f"Diff base: {base_ref} ({base_commit})"))
    lines.extend(_format_changed_files(state))

    detected = _detected_category_labels(state.categories)
    if detected:
        lines.extend(("", "Detected change types: " + ", ".join(detected)))

    lines.extend(_format_target_summary(state))

    failures = [c for c in state.commands if int(c.get("exit_code", 0)) != 0]
    for command in failures:
        lines.extend(_format_command_failure(command))

    lines.extend((
        "",
        "Fix the failures above. The checks will re-run at the end of the next turn.",
    ))
    return "\n".join(lines)


def block_and_print(state: HookState) -> int:
    """Emit a blocking response and return a stop code.

    Parameters
    ----------
    state
        Hook execution state.

    Returns
    -------
    int
        Exit code for the hook.

    """
    payload = {"decision": "block", "reason": format_reason(state)}
    print(json.dumps(payload))
    return 0


def targets_for_categories(
    categories: dict[str, bool],
    *,
    include: set[str] | None = None,
) -> list[str]:
    """Expand enabled categories into build targets.

    Parameters
    ----------
    categories
        Mapping of category flags.
    include
        Optional subset of categories to include.

    Returns
    -------
    list[str]
        Deduplicated target list.

    """
    requested: list[str] = []
    for category, enabled in categories.items():
        if not enabled:
            continue
        if include is not None and category not in include:
            continue
        requested.extend(CATS_TO_TARGETS.get(category, []))
    return dedup_preserve_order(requested)


def _is_executable_available(executable: str) -> bool:
    """Return whether an executable can be invoked."""
    return shutil.which(executable, path=_subprocess_env()["PATH"]) is not None


def _driver_error(driver: BuildDriver, *, reason: str) -> str:
    """Format a build-driver selection error."""
    return f"Cannot use {driver.name}: {reason}"


def select_build_driver(
    repo: Path, options: StopCheckOptions
) -> tuple[BuildDriver | None, str | None]:
    """Select the build driver for repository quality gates.

    Parameters
    ----------
    repo
        Repository root path.
    options
        Stop-hook runtime options.

    Returns
    -------
    tuple[BuildDriver | None, str | None]
        Selected build driver and error message, if no driver can be selected.

    """
    requested_driver = options.build_driver.strip().lower()
    if requested_driver not in SUPPORTED_BUILD_DRIVERS:
        supported = ", ".join(sorted(SUPPORTED_BUILD_DRIVERS))
        return (
            None,
            f"Unsupported build driver '{options.build_driver}'. Use {supported}.",
        )

    netsuke = BuildDriver("netsuke", options.netsuke_bin, "Netsukefile")
    make = BuildDriver("make", options.make_bin, "Makefile")
    availability = DriverAvailability(
        netsuke=netsuke,
        make=make,
        has_netsukefile=(repo / netsuke.manifest).is_file(),
        has_makefile=(repo / make.manifest).is_file(),
        has_netsuke=_is_executable_available(netsuke.executable),
        has_make=_is_executable_available(make.executable),
        has_unusable_netsukefile=(repo / netsuke.manifest).is_file()
        and not _is_executable_available(netsuke.executable),
    )
    if requested_driver == "netsuke":
        result = _select_required_driver(repo, netsuke)
    elif requested_driver == "make":
        result = _select_required_driver(repo, make)
    else:
        result = _select_auto_driver(availability)

    return result


def _select_auto_driver(
    availability: DriverAvailability,
) -> tuple[BuildDriver | None, str | None]:
    """Select a build driver using automatic discovery.

    Parameters
    ----------
    availability
        Driver availability information for the repository.

    Returns
    -------
    tuple[BuildDriver | None, str | None]
        Selected driver and error message, if any.

    """
    selected: BuildDriver | None = None
    error: str | None = None

    if availability.has_netsukefile and availability.has_netsuke:
        selected = availability.netsuke
    elif availability.has_makefile and availability.has_make:
        selected = availability.make
    elif availability.has_unusable_netsukefile and not availability.has_makefile:
        error = _driver_error(
            availability.netsuke,
            reason=f"{availability.netsuke.executable} not found",
        )
    else:
        error = (
            "No supported build driver available. Add a Netsukefile with netsuke "
            "on PATH, or add a Makefile with make on PATH."
        )

    return selected, error


def _select_required_driver(
    repo: Path, driver: BuildDriver
) -> tuple[BuildDriver | None, str | None]:
    """Select an explicitly requested driver or explain why it cannot run.

    Parameters
    ----------
    repo
        Repository root path.
    driver
        Requested build driver.

    Returns
    -------
    tuple[BuildDriver | None, str | None]
        Selected driver and error message, if any.

    """
    if not (repo / driver.manifest).is_file():
        return None, _driver_error(driver, reason=f"{driver.manifest} is missing")
    if not _is_executable_available(driver.executable):
        return None, _driver_error(driver, reason=f"{driver.executable} not found")
    return driver, None


def parse_bool_env(value: str) -> bool:
    """Parse a boolean environment value.

    Parameters
    ----------
    value
        Raw environment value.

    Returns
    -------
    bool
        True when the value is a recognized truthy token.

    """
    return value.strip().lower() in TRUTHY_VALUES


def parse_max_output(value: str, default: int = 12000) -> int:
    """Parse the max output character limit.

    Parameters
    ----------
    value
        Raw environment value.
    default
        Default value to use on parse failure.

    Returns
    -------
    int
        Parsed maximum output length.

    """
    try:
        return int(value)
    except ValueError:
        return default


def parse_env() -> tuple[str, StopCheckOptions]:
    """Parse environment configuration for the hook.

    Returns
    -------
    tuple[str, StopCheckOptions]
        Base ref and stop-check options.

    """
    base_ref = os.environ.get("POST_TURN_BASE_REF", "origin/main")
    always_fetch = parse_bool_env(os.environ.get("POST_TURN_ALWAYS_FETCH", ""))
    max_out = parse_max_output(os.environ.get("POST_TURN_MAX_OUTPUT_CHARS", "12000"))
    compush = parse_bool_env(os.environ.get("POST_TURN_COMPUSH", ""))
    return (
        base_ref,
        StopCheckOptions(
            always_fetch=always_fetch,
            max_out=max_out,
            compush=compush,
            build_driver=os.environ.get("POST_TURN_BUILD_DRIVER", "auto"),
            netsuke_bin=os.environ.get("POST_TURN_NETSUKE_BIN", "netsuke"),
            make_bin=os.environ.get("POST_TURN_MAKE_BIN", "make"),
        ),
    )


def parse_hook_input() -> dict[str, typ.Any]:
    """Parse hook input from stdin.

    Returns
    -------
    dict[str, Any]
        Parsed hook input as a dict (empty if missing or invalid).

    """
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError, ValueError:
        return {}
    match hook_input:
        case dict() as data:
            return data
        case _:
            return {}


def resolve_start_cwd(hook_input: dict[str, typ.Any]) -> Path:
    """Resolve the starting working directory for the hook.

    Parameters
    ----------
    hook_input
        Parsed hook input.

    Returns
    -------
    Path
        Working directory for git operations.

    """
    match hook_input.get("cwd"):
        case str() as cwd_value if cwd_value:
            return Path(cwd_value)
        case _:
            pass

    match os.environ.get("CLAUDE_PROJECT_DIR"):
        case str() as cwd_value if cwd_value:
            return Path(cwd_value)
        case _:
            return Path.cwd()


def fail_state(state: HookState, message: str | None) -> int:
    """Mark the state as failed and emit a block response.

    Parameters
    ----------
    state
        Hook execution state.
    message
        Error message to include in the response.

    Returns
    -------
    int
        Exit code for the hook.

    """
    state.ok = False
    state.error = message
    return block_and_print(state)


def evaluate_changes(
    state: HookState, repo: Path, max_out: int, driver: BuildDriver
) -> int:
    """Select and execute checks based on detected changes.

    Parameters
    ----------
    state
        Hook execution state.
    repo
        Repository root path.
    max_out
        Maximum number of output characters to capture.
    driver
        Build driver selected for quality gates.

    Returns
    -------
    int
        Exit code for the hook.

    """
    cats = detect_categories(state.changed_files)
    state.categories = cats
    state.build_driver = driver.name

    requested = targets_for_categories(cats)
    state.targets_requested = requested
    if not requested:
        return 0

    available_targets, target_err = get_build_targets(repo, driver)
    if available_targets is None:
        fail_state(state, f"Could not enumerate build targets: {target_err}")
        return BLOCKED_STATUS

    run_targets = [t for t in requested if t in available_targets]
    skip_targets = [t for t in requested if t not in available_targets]
    state.targets_run = run_targets
    state.targets_skipped = skip_targets

    commands: list[CommandResult] = []
    code_targets = [
        t
        for t in targets_for_categories(cats, include=CODE_CATS)
        if t in available_targets
    ]
    md_targets = [
        t
        for t in targets_for_categories(cats, include=MD_CATS)
        if t in available_targets
    ]

    if code_targets:
        commands.append(
            run_build_targets(
                repo,
                BuildTargetRequest(driver, "code", code_targets),
                max_out,
            )
        )
    if md_targets:
        commands.append(
            run_build_targets(
                repo,
                BuildTargetRequest(driver, "markdown", md_targets),
                max_out,
            )
        )

    state.commands = commands

    if not commands:
        return 0

    ok_all = all(int(c.get("exit_code", 0)) == 0 for c in commands)
    if ok_all:
        return 0

    state.ok = False
    block_and_print(state)
    return BLOCKED_STATUS


def prepare_run_stop_checks(
    start_cwd: Path, base_ref: str, *, always_fetch: bool
) -> RunStopChecksPreparation:
    """Prepare repository state for ``run_stop_checks``.

    Parameters
    ----------
    start_cwd
        Working directory for git operations.
    base_ref
        Base git ref used for comparisons.
    always_fetch
        Whether to always fetch the base ref.

    Returns
    -------
    RunStopChecksPreparation
        Structured preparation result containing the populated hook state and
        repository root when preparation succeeded.

    """
    state = HookState(base_ref=base_ref)

    if not _is_executable_available("git"):
        return RunStopChecksPreparation(
            ok=False,
            exit_code=fail_state(state, "git not found on PATH"),
            state=state,
        )

    repo, err = repo_root(start_cwd)
    if repo is None:
        exit_code = (
            0
            if not start_cwd.exists()
            or (err and "not a git repository" in err.casefold())
            else fail_state(state, err)
        )
        return RunStopChecksPreparation(ok=False, exit_code=exit_code, state=state)

    ok, err, fetched = ensure_base_ref(repo, base_ref, always_fetch=always_fetch)
    state.fetched = fetched
    if not ok:
        return RunStopChecksPreparation(
            ok=False,
            exit_code=fail_state(state, err),
            state=state,
        )

    base_commit, err = merge_base(repo, base_ref)
    if base_commit is None:
        return RunStopChecksPreparation(
            ok=False,
            exit_code=fail_state(state, err),
            state=state,
        )
    state.base_commit = base_commit

    files, err = changed_files(repo, base_commit)
    if files is None:
        return RunStopChecksPreparation(
            ok=False,
            exit_code=fail_state(state, err),
            state=state,
        )

    state.changed_files = files
    return RunStopChecksPreparation(ok=True, exit_code=0, state=state, repo=repo)


def compush_check(repo: Path) -> int:
    """Block the stop with commit/push reminders when local work is not published.

    Parameters
    ----------
    repo
        Repository root path.

    Returns
    -------
    int
        Exit code for the hook (always 0 per hook contract).

    """
    upstream, _err = get_upstream_ref(repo)
    upstream_label = upstream or "origin (upstream not configured)"

    dirty, err = has_uncommitted_changes(repo)
    if err is not None:
        return 0
    if dirty:
        payload = {
            "decision": "block",
            "reason": f"Please commit and push to {upstream_label}",
        }
        print(json.dumps(payload))
        return 0

    if upstream is None:
        return 0

    ahead, err = has_unpushed_commits(repo, upstream)
    if err is not None or not ahead:
        return 0

    payload = {
        "decision": "block",
        "reason": f"Please push committed changes to {upstream_label}",
    }
    print(json.dumps(payload))
    return 0


def run_stop_checks(
    start_cwd: Path,
    base_ref: str,
    options: StopCheckOptions,
) -> int:
    """Run stop-hook checks for a given working directory.

    Parameters
    ----------
    start_cwd
        Working directory for git operations.
    base_ref
        Base git ref used for comparisons.
    options
        Runtime options for fetch, output capture, and commit/push reminders.

    Returns
    -------
    int
        Exit code for the hook.

    """
    preparation = prepare_run_stop_checks(
        start_cwd, base_ref, always_fetch=options.always_fetch
    )
    if not preparation.ok:
        return preparation.exit_code

    state = preparation.state
    repo = preparation.repo
    if repo is None:
        state.ok = False
        state.error = "internal error: repository preparation returned no repo"
        return block_and_print(state)

    if state.changed_files:
        cats = detect_categories(state.changed_files)
        requested = targets_for_categories(cats)
        if requested:
            driver, err = select_build_driver(repo, options)
            if driver is None:
                return fail_state(state, err)

            rc = evaluate_changes(state, repo, options.max_out, driver)
            if rc != 0:
                return 0 if rc == BLOCKED_STATUS else rc

    if options.compush:
        return compush_check(repo)

    return 0


def main() -> int:
    """Run the stop-hook checks.

    Returns
    -------
    int
        Exit code for the hook.

    """
    hook_input = parse_hook_input()
    start_cwd = resolve_start_cwd(hook_input)
    base_ref, options = parse_env()
    return run_stop_checks(start_cwd, base_ref, options)


if __name__ == "__main__":
    raise SystemExit(main())
