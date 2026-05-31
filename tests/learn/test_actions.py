"""Tests for the decision → rule-template conversion (SPEC §9, §4.3)."""

from __future__ import annotations

import datetime

from ynab_agent.domain.allocations import (
    ProposedCategory,
    ResolvedAllocation,
    ResolvedCategory,
    ResolvedSplit,
    ResolvedSplitLine,
)
from ynab_agent.domain.enums import DecidedBy
from ynab_agent.domain.ids import CategoryId, PersonTag
from ynab_agent.domain.money import Money
from ynab_agent.domain.proposal import Decision
from ynab_agent.learn.actions import rule_action_from_decision

_NOW = datetime.datetime(2026, 5, 31, 12, 0, tzinfo=datetime.UTC)


def _decision(
    allocation: ResolvedAllocation, memo: str | None = None
) -> Decision:
    return Decision(
        allocation=allocation,
        memo=memo,
        approved=True,
        decided_by=DecidedBy.HUMAN,
        decided_at=_NOW,
    )


def test_category_decision_converts_to_a_template_action() -> None:
    decision = _decision(
        ResolvedCategory(
            category=CategoryId("dining"), person_tag=PersonTag("matthew")
        ),
        memo="lunch",
    )
    action = rule_action_from_decision(decision)
    assert action is not None
    assert action.memo_template == "lunch"
    allocation = action.allocation
    assert isinstance(allocation, ProposedCategory)
    assert allocation.category == "dining"
    assert allocation.person_tag == "matthew"


def test_split_decision_is_declined_for_the_agent_to_shape() -> None:
    split = ResolvedSplit(
        lines=(
            ResolvedSplitLine(
                category=CategoryId("groceries"),
                amount=Money.from_milliunits(6000),
            ),
            ResolvedSplitLine(
                category=CategoryId("household"),
                amount=Money.from_milliunits(4000),
            ),
        )
    )
    assert rule_action_from_decision(_decision(split)) is None
