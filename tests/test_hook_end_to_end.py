"""Lifecycle end-to-end tests for the hook's decision log.

Each test runs the real hook process to completion and compares its decision,
standard output and persisted records. Repository and stand-in command setup
comes from the ``repo`` and ``fake_make`` fixtures.
"""

from __future__ import annotations

import json
import typing as typ

import pytest
from hook_support import (
    finish_record,
    gate_entry,
    git_output,
    hook_environment,
    read_records,
    run_hook,
    run_hook_in,
    write_fake_make,
    write_xdg_config,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_completed_allow_is_persisted_with_its_finish_record(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A passing run allows, prints nothing, and persists a matching finish."""
    state_home = tmp_path / "state"

    result = run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "", "a successful hook writes nothing to stdout"
    records = read_records(state_home)
    start = records[0]
    assert start["record_type"] == "start"
    finish = finish_record(records)
    assert finish["session_id"] == "sess-e2e"
    assert finish["decision"] == "allow"
    assert finish["exit_code"] == 0
    assert finish["run_id"] == start["run_id"]
    assert gate_entry(finish, "quality_checks")["outcome"] == "pass"
    assert gate_entry(finish, "uncommitted_changes")["outcome"] == "pass"
    assert "startup" in typ.cast("dict[str, object]", finish["stages"])


def test_completed_block_is_persisted_with_its_finish_record(
    repo: Path,
    tmp_path: Path,
) -> None:
    """A failing quality gate blocks, prints the JSON, and records the block."""
    fake_make = write_fake_make(tmp_path / "fake-make", exit_code=1)
    state_home = tmp_path / "state"

    result = run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block", payload
    finish = finish_record(read_records(state_home))
    assert finish["decision"] == "block"
    assert finish["exit_code"] == 0
    assert gate_entry(finish, "quality_checks")["outcome"] == "block"
    assert gate_entry(finish, "uncommitted_changes")["outcome"] == "not_reached", (
        "a gate after the blocking quality check must be recorded as unreached"
    )


@pytest.mark.parametrize(
    ("exit_code", "decision"),
    [(0, "allow"), (1, "block")],
    ids=["allow", "block"],
)
def test_unwritable_log_leaves_a_completed_decision_unchanged(
    repo: Path, tmp_path: Path, exit_code: int, decision: str
) -> None:
    """Completed allow and block runs match with an unwritable log."""
    fake_make = write_fake_make(tmp_path / "fake-make", exit_code=exit_code)
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")

    writable = run_hook(repo, tmp_path, tmp_path / "state", fake_make)
    unwritable = run_hook(repo, tmp_path, blocker / "state", fake_make)

    assert unwritable.returncode == writable.returncode
    assert unwritable.stdout == writable.stdout
    assert "could not write the decision log" in unwritable.stderr
    assert finish_record(read_records(tmp_path / "state"))["decision"] == decision


def test_a_configuration_opt_out_matches_logging_disabled(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """A late opt-out leaves the earlier records plus a ``disabled`` finish.

    The start and input-stage records exist before the configuration is read;
    they are written outside the worktree, where no gate can report them. The
    opt-out then writes a terminal finish marked ``disabled`` and suppresses
    every later record. The decision is the same as a run disabled from the
    start.
    """
    write_xdg_config(tmp_path, "log_enabled = false\n")
    state_home = tmp_path / "state"

    disabled_env = hook_environment(tmp_path, tmp_path / "unused-state", fake_make)
    disabled_env["POST_TURN_LOG"] = "0"
    disabled = run_hook_in(repo, disabled_env)
    opted_out = run_hook_in(repo, hook_environment(tmp_path, state_home, fake_make))

    assert opted_out.returncode == disabled.returncode == 0, opted_out.stderr
    assert opted_out.stdout == disabled.stdout == ""
    records = read_records(state_home)
    assert [record["record_type"] for record in records] == [
        "start",
        "progress",
        "progress",
        "progress",
        "finish",
    ]
    assert all(
        record.get("stage") == "input" or record.get("event") == "correlation"
        for record in records[1:-1]
    ), "the opt-out must suppress every record after the configuration load"
    finish = records[-1]
    assert finish["decision"] == "disabled"
    assert finish["exit_code"] == 0


@pytest.mark.parametrize("exit_code", [0, 1])
def test_finish_record_joins_correlation_fields(
    repo: Path,
    tmp_path: Path,
    exit_code: int,
) -> None:
    """Both outcomes keep the same run identity in every record."""
    fake_make = write_fake_make(tmp_path / "fake-make", exit_code=exit_code)
    state_home = tmp_path / "state"

    run_hook(repo, tmp_path, state_home, fake_make)

    records = read_records(state_home)
    run_ids = {record["run_id"] for record in records}
    assert len(run_ids) == 1, f"expected one run id but got {run_ids!r}"
    start = records[0]
    assert start["record_type"] == "start"
    assert "session_id" not in start, (
        "the start record is written before the hook input is read"
    )
    known = next(
        index
        for index, record in enumerate(records)
        if record.get("event") == "correlation"
    )
    assert records[known]["session_id"] == "sess-e2e"
    assert all(record["session_id"] == "sess-e2e" for record in records[known:])


def test_the_finish_record_carries_the_real_head_commit(
    repo: Path,
    fake_make: Path,
    tmp_path: Path,
) -> None:
    """The recorded ``repository.head`` is the repository's real ``HEAD``.

    The value is read back with Git after the run rather than mocked, so a
    record that reported a placeholder, another repository's commit, or an
    empty value would fail here.
    """
    state_home = tmp_path / "state"

    result = run_hook(repo, tmp_path, state_home, fake_make)

    assert result.returncode == 0, result.stderr
    expected = git_output(repo, "rev-parse", "HEAD")
    finish = finish_record(read_records(state_home))
    repository = typ.cast("dict[str, object]", finish["repository"])
    assert repository["head"] == expected, (
        f"expected the real HEAD {expected!r} but got {repository['head']!r}"
    )
    assert repository["branch"] == "feature", (
        f"expected the run's branch but got {repository['branch']!r}"
    )


def test_a_preparation_failure_emits_its_block_exactly_once(
    repo: Path,
    tmp_path: Path,
    fake_make: Path,
) -> None:
    """A preparation block is rendered by the boundary, not by preparation.

    Preparation returns a domain notice and prints nothing. The boundary emits
    that notice once, so the process still writes exactly one block payload and
    nothing else to standard output.
    """
    state_home = tmp_path / "state"
    env = hook_environment(tmp_path, state_home, fake_make)
    # A base ref that cannot resolve fails preparation before any gate runs.
    env["POST_TURN_BASE_REF"] = "refs/heads/does-not-exist"

    result = run_hook_in(repo, env)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block", payload
    assert result.stdout.count("\n") == 1, (
        "one block payload is emitted, so standard output holds one line"
    )
    assert "Error:" in payload["reason"], (
        "the response keeps the preparation diagnostic"
    )
    finish = finish_record(read_records(state_home))
    assert finish["decision"] == "block"
