"""Tests for the audit builders and the explain renderer (SPEC §9)."""

from __future__ import annotations

import datetime

from ynab_agent.audit.entry import AuditLog
from ynab_agent.audit.record import (
    allocation_summary,
    explain,
    record_budget_move,
    record_decision,
    record_gate,
    record_learning,
    record_send,
    record_transition,
)
from ynab_agent.budget.balance import BudgetMove
from ynab_agent.domain.allocations import (
    ResolvedCategory,
    ResolvedSplit,
    ResolvedSplitLine,
)
from ynab_agent.domain.enums import DecidedBy, TrustState, TxnState
from ynab_agent.domain.ids import CategoryId, RuleId
from ynab_agent.domain.money import Money
from ynab_agent.domain.proposal import Decision
from ynab_agent.learn.transitions import RuleChange, RuleChangeKind
from ynab_agent.policy.gate import GateOutcome, GateVerdict

_NOW = datetime.datetime(2026, 5, 31, 12, 0, tzinfo=datetime.UTC)


def test_allocation_summary_category_and_split() -> None:
    assert (
        allocation_summary(ResolvedCategory(category=CategoryId("dining")))
        == "category dining"
    )
    split = ResolvedSplit(
        lines=(
            ResolvedSplitLine(
                category=CategoryId("a"), amount=Money.from_milliunits(500)
            ),
            ResolvedSplitLine(
                category=CategoryId("b"), amount=Money.from_milliunits(500)
            ),
        )
    )
    assert allocation_summary(split) == "split across 2 lines"


def test_record_gate_carries_verdict_rule_and_reason() -> None:
    event = record_gate(
        GateOutcome(
            verdict=GateVerdict.AUTO, rule_id="r1", reason="single trusted"
        )
    )
    assert event.verdict == "auto"
    assert event.rule_id == "r1"
    assert event.reason == "single trusted"


def test_record_decision_summarizes_the_allocation() -> None:
    decision = Decision(
        allocation=ResolvedCategory(category=CategoryId("dining")),
        approved=True,
        decided_by=DecidedBy.AGENT,
        decided_at=_NOW,
        rule_id=RuleId("r1"),
    )
    event = record_decision(decision)
    assert event.decided_by is DecidedBy.AGENT
    assert event.approved is True
    assert event.summary == "category dining"
    assert event.rule_id == "r1"


def test_record_send_carries_seq_and_purpose() -> None:
    # Sends are recorded before going out — the (txn, action_seq) dedup key.
    event = record_send(3, "ask")
    assert event.action_seq == 3
    assert event.purpose == "ask"


def test_record_learning_carries_change_and_trust() -> None:
    change = RuleChange(
        kind=RuleChangeKind.STRENGTHENED,
        rule_id=RuleId("r1"),
        trust=TrustState.TRUSTED,
    )
    event = record_learning(change)
    assert event.change == "strengthened"
    assert event.rule_id == "r1"
    assert event.trust is TrustState.TRUSTED


def test_record_budget_move_carries_amount_and_categories() -> None:
    event = record_budget_move(
        BudgetMove(
            source=CategoryId("buffer"),
            destination=CategoryId("dining"),
            amount=Money.from_currency("120"),
        ),
        month="current",
    )
    assert event.source == "buffer"
    assert event.destination == "dining"
    assert event.amount_milliunits == 120000
    assert event.month == "current"


def test_explain_renders_a_budget_move() -> None:
    log = AuditLog().append(
        record_budget_move(
            BudgetMove(
                source=CategoryId("buffer"),
                destination=CategoryId("dining"),
                amount=Money.from_currency("120"),
            ),
            month="current",
        ),
        at=_NOW,
    )
    assert "budget move: $120.00 buffer -> dining (current)" in explain(log)


def test_explain_renders_a_readable_trail() -> None:
    log = (
        AuditLog()
        .append(
            record_transition(to_state=TxnState.ENRICHING, trigger="snapshot"),
            at=_NOW,
        )
        .append(
            record_gate(GateOutcome(verdict=GateVerdict.ASK, reason="no rule")),
            at=_NOW,
        )
    )
    trail = explain(log)
    assert "# 0" in trail
    assert "-> enriching (on snapshot)" in trail
    assert "gate: ask - no rule" in trail
