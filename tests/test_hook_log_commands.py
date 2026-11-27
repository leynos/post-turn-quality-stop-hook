"""End-to-end tests for recording fetch commands in the decision log.

A run that fetches records the command's start and its exit code; a run that
does not fetch records nothing, so the log never invents command history.
"""

from __future__ import annotations

import json
import typing as typ

from hook_support import (
    fetch_commands,
    finish_record,
    git_run,
    hook_environment,
    read_records,
    run_hook_in,
    write_xdg_config,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_real_fetch_is_recorded_with_its_exit_code(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A real fetch from a local remote leaves progress and a finish command."""
    origin = tmp_path / "origin.git"
    git_run(repo, "init", "--bare", "-q", str(origin))
    git_run(repo, "remote", "add", "origin", str(origin))
    git_run(repo, "push", "-q", "origin", "main")
    write_xdg_config(tmp_path, "gate_pr_rebase = false\n")
    state_home = tmp_path / "state"
    env = hook_environment(tmp_path, state_home, fake_make)
    env.update({
        "POST_TURN_ALWAYS_FETCH": "1",
        "POST_TURN_BASE_REF": "origin/main",
    })

    result = run_hook_in(repo, env)

    assert result.returncode == 0, result.stderr
    records = read_records(state_home)
    starts = [
        record
        for record in records
        if record.get("event") == "command_start"
        and typ.cast("dict[str, object]", record["command"])["kind"] == "fetch"
    ]
    assert len(starts) == 1, f"expected one recorded fetch start but got {starts!r}"
    assert [command["exit_code"] for command in fetch_commands(records)] == [0]
    assert finish_record(records)["decision"] == "allow"


def test_failed_real_fetch_is_recorded_with_its_exit_code(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A missing remote blocks and records the fetch's non-zero exit code."""
    git_run(repo, "remote", "add", "origin", str(tmp_path / "missing.git"))
    write_xdg_config(tmp_path, "gate_pr_rebase = false\n")
    state_home = tmp_path / "state"
    env = hook_environment(tmp_path, state_home, fake_make)
    env.update({
        "POST_TURN_ALWAYS_FETCH": "1",
        "POST_TURN_BASE_REF": "origin/main",
    })

    result = run_hook_in(repo, env)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    records = read_records(state_home)
    fetches = fetch_commands(records)
    assert len(fetches) == 1, f"expected one fetch but got {fetches!r}"
    assert fetches[0]["exit_code"] != 0, "a failed fetch must record its status"
    assert finish_record(records)["decision"] == "block"


def test_run_without_a_fetch_records_no_fetch_command(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A local base ref runs no fetch, so none is recorded."""
    state_home = tmp_path / "state"
    env = hook_environment(tmp_path, state_home, fake_make)
    env["POST_TURN_ALWAYS_FETCH"] = "0"

    result = run_hook_in(repo, env)

    assert result.returncode == 0, result.stderr
    assert fetch_commands(read_records(state_home)) == []
