"""Tests for the W7 budget balancer's pure coverage planner (SPEC §8)."""

from __future__ import annotations

from ynab_agent.budget.balance import (
    BudgetMove,
    Need,
    Source,
    SourcePriority,
    plan_coverage,
)
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money


def _need(category: str, amount: str) -> Need:
    return Need(
        category=CategoryId(category), shortfall=Money.from_currency(amount)
    )


def _source(category: str, amount: str, priority: SourcePriority) -> Source:
    return Source(
        category=CategoryId(category),
        available=Money.from_currency(amount),
        priority=priority,
    )


def test_single_need_covered_from_one_source() -> None:
    plan = plan_coverage(
        [_need("dining", "120")],
        [_source("buffer", "500", SourcePriority.BUFFER)],
    )
    assert plan.fully_covered
    assert plan.moves == (
        BudgetMove(
            source=CategoryId("buffer"),
            destination=CategoryId("dining"),
            amount=Money.from_currency("120"),
        ),
    )


def test_sources_drawn_in_priority_order() -> None:
    # Ready-to-Assign is exhausted before the buffer is touched.
    plan = plan_coverage(
        [_need("dining", "120")],
        [
            _source("buffer", "500", SourcePriority.BUFFER),
            _source("rta", "100", SourcePriority.READY_TO_ASSIGN),
        ],
    )
    assert plan.fully_covered
    assert plan.moves[0].source == "rta"
    assert plan.moves[0].amount == Money.from_currency("100")
    assert plan.moves[1].source == "buffer"
    assert plan.moves[1].amount == Money.from_currency("20")


def test_partial_coverage_reports_uncovered() -> None:
    plan = plan_coverage(
        [_need("dining", "300")],
        [_source("rta", "100", SourcePriority.READY_TO_ASSIGN)],
    )
    assert not plan.fully_covered
    assert plan.uncovered[0].category == "dining"
    assert plan.uncovered[0].shortfall == Money.from_currency("200")


def test_one_source_split_across_two_needs() -> None:
    plan = plan_coverage(
        [_need("dining", "60"), _need("gas", "60")],
        [_source("rta", "100", SourcePriority.READY_TO_ASSIGN)],
    )
    # The first need takes $60, the second only $40 is left → $20 uncovered.
    assert plan.moves[0].destination == "dining"
    assert plan.moves[0].amount == Money.from_currency("60")
    assert plan.moves[1].destination == "gas"
    assert plan.moves[1].amount == Money.from_currency("40")
    assert plan.uncovered[0].shortfall == Money.from_currency("20")


def test_no_needs_is_an_empty_plan() -> None:
    plan = plan_coverage(
        [], [_source("rta", "100", SourcePriority.READY_TO_ASSIGN)]
    )
    assert plan.moves == ()
    assert plan.fully_covered
