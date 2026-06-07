"""Exercise bundled Jinja templates."""

from __future__ import annotations

from pathlib import Path

from post_turn_quality_stop_hook.templates import render


def test_rebase_template_matches_canonical_copy() -> None:
    """Bundled rebase template stays byte-for-byte aligned with docs copy."""
    canonical = Path("docs/templates/rebase_required.j2").read_text(encoding="utf-8")
    bundled = Path(
        "post_turn_quality_stop_hook/templates/rebase_required.j2"
    ).read_text(encoding="utf-8")
    assert bundled == canonical, (
        f"expected bundled template to match canonical docs copy; "
        f"bundled={bundled!r}, canonical={canonical!r}"
    )


def test_rebase_template_renders_typecheck_condition() -> None:
    """The rendered rebase message includes typecheck only when available."""
    reason_with_typecheck = render(
        "rebase_required.j2",
        primary_remote="origin",
        base_branch="main",
        three_way_merge_is_configured=False,
        makefile_has_typecheck_target=True,
    )
    reason_without_typecheck = render(
        "rebase_required.j2",
        primary_remote="origin",
        base_branch="main",
        three_way_merge_is_configured=False,
        makefile_has_typecheck_target=False,
    )
    assert "Please rebase this branch onto `origin/main`" in reason_with_typecheck, (
        "expected rendered rebase reason to name origin/main; "
        f"reason={reason_with_typecheck!r}"
    )
    assert "`make typecheck`" in reason_with_typecheck, (
        "expected typecheck command when makefile_has_typecheck_target=True; "
        f"reason={reason_with_typecheck!r}"
    )
    assert "`make typecheck`" not in reason_without_typecheck, (
        "expected no typecheck command when makefile_has_typecheck_target=False; "
        f"reason={reason_without_typecheck!r}"
    )


def test_uncommitted_template_renders_commit_instruction() -> None:
    """The uncommitted template instructs the agent to commit."""
    reason = render("uncommitted_required.j2")
    assert "Please commit outstanding changes" in reason, (
        f"expected uncommitted template to instruct committing; reason={reason!r}"
    )


def test_unpushed_template_renders_upstream() -> None:
    """The unpushed template names the tracked upstream branch."""
    upstream_ref = "origin/feature"
    reason = render("unpushed_required.j2", upstream_ref=upstream_ref)
    assert upstream_ref in reason, (
        "expected unpushed template to name upstream ref; "
        f"upstream_ref={upstream_ref!r}, reason={reason!r}"
    )
