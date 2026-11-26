"""Property tests for output truncation and ordered deduplication.

Both helpers carry an invariant that example tests can only sample: a
truncated result must fit the budget it was given, and deduplication must keep
each item's first occurrence in the input's order. The properties below state
those invariants over a broad input space and pair them with the boundaries
the implementation distinguishes — a non-positive budget, the truncation
marker's own length, and a marker-free prefix cut — so the boundary cases are
named rather than left to chance.
"""

from __future__ import annotations

import itertools

from hypothesis import example, given
from hypothesis import strategies as st

from post_turn_quality_stop_hook.execution import truncate
from post_turn_quality_stop_hook.quality_policy import dedup_preserve_order

# The marker `truncate` inserts between the kept head and the kept tail. Its
# length is the smallest budget that can hold a marker at all.
TRUNCATION_MARKER = "\n... (output truncated) ...\n"

# Long enough to be truncated by every budget this module generates.
LONG_TEXT = "abcdefghij" * 40

# Small enough to collide often, so duplicates dominate the generated lists.
DUPLICATE_PRONE_ITEMS = st.lists(
    st.one_of(
        st.sampled_from(("check-fmt", "lint", "typecheck", "markdownlint")),
        st.text(max_size=4),
    ),
    max_size=40,
)


@st.composite
def fitting_truncations(draw: st.DrawFn) -> tuple[str, int]:
    """Draw a text together with a budget it already fits."""
    max_chars = draw(st.integers(min_value=0, max_value=200))
    return draw(st.text(max_size=max_chars)), max_chars


@st.composite
def marker_truncations(draw: st.DrawFn) -> tuple[str, int]:
    """Draw a text longer than a budget that leaves room for the marker.

    The budget and the text are drawn together because the branch under test
    only exists while the text is strictly longer than the budget; drawing
    them independently would reject most candidates.
    """
    max_chars = draw(st.integers(min_value=len(TRUNCATION_MARKER) + 1, max_value=200))
    text = draw(st.text(min_size=max_chars + 1, max_size=300))
    return text, max_chars


@given(
    text=st.text(max_size=300),
    max_chars=st.integers(min_value=0, max_value=200),
)
def test_output_never_exceeds_the_limit(text: str, max_chars: int) -> None:
    """A non-negative budget bounds the length of the returned text."""
    result = truncate(text, max_chars)

    assert len(result) <= max_chars


@given(
    text=st.text(max_size=300),
    max_chars=st.integers(max_value=0),
)
@example(text=LONG_TEXT, max_chars=-1)
@example(text=LONG_TEXT, max_chars=0)
def test_non_positive_limit_returns_empty(text: str, max_chars: int) -> None:
    """A budget with no room for characters yields no output at all."""
    assert truncate(text, max_chars) == ""


@given(fitting_truncations())
def test_input_within_the_limit_is_returned_unchanged(case: tuple[str, int]) -> None:
    """Text that already fits its budget is returned verbatim."""
    text, max_chars = case

    assert truncate(text, max_chars) == text


@given(marker_truncations())
@example(case=(LONG_TEXT, len(TRUNCATION_MARKER) + 1))
def test_truncation_above_the_marker_budget_keeps_head_and_tail(
    case: tuple[str, int],
) -> None:
    """Above the marker budget the output is a head, the marker, then a tail.

    The result fills the budget exactly. The head is the input's leading text,
    the tail is its trailing text, and the text between them is what
    truncation removed.
    """
    text, max_chars = case

    result = truncate(text, max_chars)

    budget = max_chars - len(TRUNCATION_MARKER)
    head_len = budget // 2
    tail_len = budget - head_len
    marker_end = head_len + len(TRUNCATION_MARKER)
    assert len(result) == max_chars, (
        f"expected the result to fill {max_chars} characters but got {len(result)}"
    )
    assert result[head_len:marker_end] == TRUNCATION_MARKER, (
        f"expected the marker at offset {head_len} but got {result!r}"
    )
    assert result[:head_len] == text[:head_len], (
        f"expected the input's leading text but got {result[:head_len]!r}"
    )
    assert result[marker_end:] == text[-tail_len:], (
        f"expected the input's trailing text but got {result[marker_end:]!r}"
    )
    assert text[head_len : len(text) - tail_len], (
        "expected truncation to drop part of the input"
    )


@given(
    max_chars=st.integers(min_value=1, max_value=len(TRUNCATION_MARKER)),
    text=st.text(min_size=len(TRUNCATION_MARKER) + 1, max_size=300),
)
@example(text=LONG_TEXT, max_chars=len(TRUNCATION_MARKER))
def test_truncation_within_the_marker_budget_is_a_plain_prefix(
    text: str, max_chars: int
) -> None:
    """A budget too small for the marker keeps the input's leading text only."""
    result = truncate(text, max_chars)

    assert result == text[:max_chars]
    assert len(result) == max_chars


@given(
    text=st.text(max_size=300),
    max_chars=st.integers(min_value=0, max_value=200),
)
def test_truncation_is_idempotent(text: str, max_chars: int) -> None:
    """Truncating an already truncated result changes nothing further."""
    once = truncate(text, max_chars)

    assert truncate(once, max_chars) == once


@given(DUPLICATE_PRONE_ITEMS)
def test_dedup_shortens_input_and_leaves_no_duplicates(items: list[str]) -> None:
    """Deduplication never grows the list and leaves each item at most once."""
    result = dedup_preserve_order(items)

    assert len(result) <= len(items)
    assert len(set(result)) == len(result)


@given(DUPLICATE_PRONE_ITEMS)
def test_dedup_keeps_every_distinct_item(items: list[str]) -> None:
    """Deduplication drops repeats rather than items."""
    assert set(dedup_preserve_order(items)) == set(items)


@given(DUPLICATE_PRONE_ITEMS)
def test_dedup_keeps_first_occurrence_order(items: list[str]) -> None:
    """Each kept item appears in the order its first occurrence did."""
    result = dedup_preserve_order(items)

    first_positions = [items.index(item) for item in result]
    assert all(
        earlier < later for earlier, later in itertools.pairwise(first_positions)
    ), f"expected first occurrences in input order but got {result!r}"


@given(DUPLICATE_PRONE_ITEMS)
def test_dedup_is_idempotent(items: list[str]) -> None:
    """Deduplicating an already deduplicated list changes nothing."""
    once = dedup_preserve_order(items)

    assert dedup_preserve_order(once) == once


@given(DUPLICATE_PRONE_ITEMS)
def test_dedup_does_not_mutate_its_input(items: list[str]) -> None:
    """The caller's list is left exactly as it was passed in."""
    original = list(items)

    dedup_preserve_order(items)

    assert items == original
