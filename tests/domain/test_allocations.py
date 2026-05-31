"""Tests for allocations: the category-XOR-split modeling and its invariants."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from ynab_agent.domain.allocations import (
    FixedShare,
    PercentShare,
    ProposedAllocation,
    ProposedCategory,
    ProposedSplit,
    ResolvedSplit,
    ResolvedSplitLine,
    SplitLine,
)
from ynab_agent.domain.ids import CategoryId, PersonTag
from ynab_agent.domain.money import Money


def _line(percent: int, category: str) -> SplitLine:
    return SplitLine(
        share=PercentShare(percent=percent), category=CategoryId(category)
    )


def test_percent_shares_must_sum_to_100() -> None:
    with pytest.raises(ValidationError):
        ProposedSplit(lines=(_line(50, "a"), _line(40, "b")))


def test_fifty_fifty_is_valid() -> None:
    split = ProposedSplit(lines=(_line(50, "a"), _line(50, "b")))
    assert len(split.lines) == 2


def test_fixed_plus_remainder_is_valid() -> None:
    # "$40 Gifts, rest Groceries": one fixed line, one 100% percent line.
    split = ProposedSplit(
        lines=(
            SplitLine(
                share=FixedShare(amount=Money.from_currency(40)),
                category=CategoryId("gifts"),
            ),
            SplitLine(
                share=PercentShare(percent=100),
                category=CategoryId("groceries"),
            ),
        )
    )
    assert len(split.lines) == 2


def test_split_needs_at_least_two_lines() -> None:
    with pytest.raises(ValidationError):
        ProposedSplit(lines=(_line(100, "a"),))


def test_all_fixed_split_rejected() -> None:
    # No percent line to absorb the remainder (SPEC §1 fixed-then-percent).
    with pytest.raises(ValidationError):
        ProposedSplit(
            lines=(
                SplitLine(
                    share=FixedShare(amount=Money.from_currency(30)),
                    category=CategoryId("a"),
                ),
                SplitLine(
                    share=FixedShare(amount=Money.from_currency(10)),
                    category=CategoryId("b"),
                ),
            )
        )


def test_category_can_carry_person_tag() -> None:
    cat = ProposedCategory(
        category=CategoryId("dining"), person_tag=PersonTag("matthew")
    )
    assert cat.person_tag == "matthew"


def test_percent_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        PercentShare(percent=0)
    with pytest.raises(ValidationError):
        PercentShare(percent=101)


def test_discriminated_union_dispatch() -> None:
    adapter: TypeAdapter[ProposedAllocation] = TypeAdapter(ProposedAllocation)
    parsed = adapter.validate_python({"kind": "category", "category": "dining"})
    assert isinstance(parsed, ProposedCategory)
    assert parsed.category == "dining"


def test_resolved_split_total_sums_lines() -> None:
    split = ResolvedSplit(
        lines=(
            ResolvedSplitLine(
                category=CategoryId("a"), amount=Money.from_currency(30)
            ),
            ResolvedSplitLine(
                category=CategoryId("b"), amount=Money.from_currency(10)
            ),
        )
    )
    assert split.total == Money.from_currency(40)
