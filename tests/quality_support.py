"""Shared fixtures for quality-stage and pipeline tests."""

from __future__ import annotations

import typing as typ

from post_turn_quality_stop_hook.git_facts import GitFacts

if typ.TYPE_CHECKING:
    from post_turn_quality_stop_hook import execution as exec_mod

GIT_FACTS = GitFacts(
    primary_remote="origin",
    upstream_ref="origin/feature",
    pr_base_local_ref=None,
    local_base_commit=None,
    three_way_merge_is_configured=False,
)


def command(
    kind: str = "code", exit_code: int = 0, cmd: str = "make lint"
) -> exec_mod.CommandResult:
    """Build a command result for a quality evaluation."""
    return {
        "kind": kind,
        "cmd": cmd,
        "exit_code": exit_code,
        "stdout": "" if exit_code == 0 else "boom",
        "stderr": "",
    }
