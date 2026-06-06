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
    assert bundled == canonical


def test_rebase_template_renders_typecheck_condition() -> None:
    """The rendered rebase message includes typecheck only when available."""
    reason = render(
        "rebase_required.j2",
        primary_remote="origin",
        base_branch="main",
        three_way_merge_is_configured=False,
        makefile_has_typecheck_target=True,
    )
    assert "Please rebase this branch onto `origin/main`" in reason
    assert "`make typecheck`" in reason
