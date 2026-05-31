"""Tests for the allocation resolver (SPEC §1 fixed-then-percent)."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from ynab_agent.domain.allocations import (
    FixedShare,
    PercentShare,
    ProposedCategory,
    ProposedSplit,
    ResolvedCategory,
    ResolvedSplit,
    SplitLine,
)
from ynab_agent.domain.ids import CategoryId, PersonTag
from ynab_agent.domain.money import Money
from ynab_agent.policy.resolve import resolve_allocation


def _pct(percent: int, category: str) -> SplitLine:
    return SplitLine(
        share=PercentShare(percent=percent), category=CategoryId(category)
    )


def test_category_passthrough_preserves_person_tag() -> None:
    proposed = ProposedCategory(
        category=CategoryId("dining"), person_tag=PersonTag("matthew")
    )
    resolved = resolve_allocation(proposed, Money.from_currency("-4.50"))
    assert isinstance(resolved, ResolvedCategory)
    assert resolved.person_tag == "matthew"


def test_fifty_fifty_splits_evenly() -> None:
    split = ProposedSplit(lines=(_pct(50, "a"), _pct(50, "b")))
    resolved = resolve_allocation(split, Money.from_currency("-4.50"))
    assert isinstance(resolved, ResolvedSplit)
    assert [line.amount.milliunits for line in resolved.lines] == [-2250, -2250]


def test_fixed_then_remainder() -> None:
    # "$40 Gifts, rest Groceries" on a $120 transaction.
    split = ProposedSplit(
        lines=(
            SplitLine(
                share=FixedShare(amount=Money.from_currency(40)),
                category=CategoryId("gifts"),
            ),
            _pct(100, "groceries"),
        )
    )
    resolved = resolve_allocation(split, Money.from_currency(120))
    assert isinstance(resolved, ResolvedSplit)
    assert [line.amount.milliunits for line in resolved.lines] == [40000, 80000]


def test_fixed_then_remainder_on_an_outflow() -> None:
    # The common case: positive-magnitude fixed line on a negative outflow.
    # "$40 Gifts, rest Groceries" on a -$120 charge → both lines are outflows.
    split = ProposedSplit(
        lines=(
            SplitLine(
                share=FixedShare(amount=Money.from_currency(40)),
                category=CategoryId("gifts"),
            ),
            _pct(100, "groceries"),
        )
    )
    resolved = resolve_allocation(split, Money.from_currency("-120"))
    assert isinstance(resolved, ResolvedSplit)
    amounts = [line.amount.milliunits for line in resolved.lines]
    assert amounts == [-40000, -80000]


@given(total_mu=st.integers(min_value=-1_000_000, max_value=1_000_000))
def test_split_lines_always_sum_to_total(total_mu: int) -> None:
    # The residue-absorbing last percent line guarantees an exact sum for ANY
    # total, including ones the percents do not divide evenly.
    split = ProposedSplit(lines=(_pct(33, "a"), _pct(33, "b"), _pct(34, "c")))
    resolved = resolve_allocation(split, Money.from_milliunits(total_mu))
    assert isinstance(resolved, ResolvedSplit)
    assert sum(line.amount.milliunits for line in resolved.lines) == total_mu
