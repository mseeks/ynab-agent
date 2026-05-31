"""Tests for the W5 effect → rule-table adapter (SPEC §9)."""

from __future__ import annotations

import datetime

from ynab_agent.domain.allocations import (
    ProposedCategory,
    ResolvedAllocation,
    ResolvedCategory,
    ResolvedSplit,
    ResolvedSplitLine,
)
from ynab_agent.domain.effects import FeedRuleLearning, RuleLearningKind
from ynab_agent.domain.enums import DecidedBy, RuleSource, TrustState
from ynab_agent.domain.ids import CategoryId, RuleId
from ynab_agent.domain.money import Money
from ynab_agent.domain.proposal import Decision
from ynab_agent.domain.rule import Rule, RuleAction, RuleMatch
from ynab_agent.learn.handler import plan_rule_update

_NOW = datetime.datetime(2026, 5, 31, 12, 0, tzinfo=datetime.UTC)


def _decision(
    allocation: ResolvedAllocation, rule_id: RuleId | None = None
) -> Decision:
    return Decision(
        allocation=allocation,
        approved=True,
        decided_by=DecidedBy.HUMAN,
        decided_at=_NOW,
        rule_id=rule_id,
    )


_DINING = ResolvedCategory(category=CategoryId("dining"))
_COFFEE = ResolvedCategory(category=CategoryId("coffee"))


def test_confirm_creates_a_rule_for_the_payee() -> None:
    feed = FeedRuleLearning(
        event=RuleLearningKind.CONFIRM,
        payee="Blue Bottle",
        decision=_decision(_DINING),
    )
    outcome = plan_rule_update((), feed, now=_NOW, next_id=RuleId("r1"))
    assert outcome is not None
    rule = outcome.rules[0]
    assert rule.match.payee_pattern == "Blue Bottle"
    assert rule.trust is TrustState.CONFIRMED


def test_correct_demotes_the_prior_rule() -> None:
    prior = Rule(
        id=RuleId("r1"),
        match=RuleMatch(payee_pattern="Blue Bottle"),
        action=RuleAction(
            allocation=ProposedCategory(category=CategoryId("dining"))
        ),
        trust=TrustState.TRUSTED,
        hits=5,
        source=RuleSource.LEARNED,
    )
    feed = FeedRuleLearning(
        event=RuleLearningKind.CORRECT,
        payee="Blue Bottle",
        decision=_decision(_COFFEE),
        prior=_decision(_DINING, rule_id=RuleId("r1")),
    )
    outcome = plan_rule_update(
        (prior,), feed, now=_NOW, next_id=RuleId("unused")
    )
    assert outcome is not None
    assert outcome.rules[0].trust is TrustState.SUGGESTED


def test_no_decision_learns_nothing() -> None:
    feed = FeedRuleLearning(
        event=RuleLearningKind.CONFIRM, payee="Blue Bottle", decision=None
    )
    assert plan_rule_update((), feed, now=_NOW, next_id=RuleId("r1")) is None


def test_split_decision_is_declined() -> None:
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
    feed = FeedRuleLearning(
        event=RuleLearningKind.CONFIRM,
        payee="Costco",
        decision=_decision(split),
    )
    assert plan_rule_update((), feed, now=_NOW, next_id=RuleId("r1")) is None
