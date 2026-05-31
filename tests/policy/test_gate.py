"""Tests for the autonomy gate and rule matching (SPEC §4.2, §1)."""

from __future__ import annotations

import datetime

from ynab_agent.domain.allocations import ProposedCategory, ResolvedCategory
from ynab_agent.domain.enums import DecidedBy, RuleSource, TrustState
from ynab_agent.domain.ids import (
    AccountId,
    CategoryId,
    RuleId,
    YnabTransactionId,
)
from ynab_agent.domain.money import Money
from ynab_agent.domain.rule import AmountRange, Rule, RuleAction, RuleMatch
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.policy.floor import AutoActionCounters
from ynab_agent.policy.gate import (
    GateVerdict,
    build_auto_decision,
    evaluate_gate,
    rule_matches,
)

_EPOCH = datetime.datetime(2026, 5, 28, tzinfo=datetime.UTC)


def _snapshot(**kw: object) -> YnabSnapshot:
    base: dict[str, object] = {
        "ynab_id": YnabTransactionId("t1"),
        "account": AccountId("a1"),
        "payee": "Blue Bottle Coffee",
        "amount": Money.from_currency("-4.50"),
        "txn_date": datetime.date(2026, 5, 28),
    }
    base.update(kw)
    return YnabSnapshot.model_validate(base)


def _rule(
    trust: TrustState,
    *,
    payee: str = "Blue Bottle",
    category: str = "dining",
    rid: str = "r1",
    match: RuleMatch | None = None,
) -> Rule:
    return Rule(
        id=RuleId(rid),
        match=match or RuleMatch(payee_pattern=payee),
        action=RuleAction(
            allocation=ProposedCategory(category=CategoryId(category))
        ),
        trust=trust,
        source=RuleSource.LEARNED,
    )


def test_rule_matches_payee_substring_case_insensitive() -> None:
    assert rule_matches(
        _rule(TrustState.TRUSTED, payee="blue bottle"), _snapshot()
    )


def test_rule_does_not_match_other_payee() -> None:
    snap = _snapshot(payee="Costco")
    assert not rule_matches(_rule(TrustState.TRUSTED, payee="Amazon"), snap)


def test_rule_matches_amount_range() -> None:
    band = RuleMatch(
        payee_pattern="Blue Bottle",
        amount_range=AmountRange(
            low=Money.from_currency("-10"), high=Money.from_currency("0")
        ),
    )
    assert rule_matches(_rule(TrustState.TRUSTED, match=band), _snapshot())


def test_single_trusted_rule_is_auto() -> None:
    out = evaluate_gate(
        _snapshot(), [_rule(TrustState.TRUSTED)], AutoActionCounters()
    )
    assert out.verdict is GateVerdict.AUTO
    assert out.rule_id == "r1"


def test_no_trusted_rule_asks() -> None:
    out = evaluate_gate(
        _snapshot(), [_rule(TrustState.CONFIRMED)], AutoActionCounters()
    )
    assert out.verdict is GateVerdict.ASK


def test_conflicting_trusted_rules_ask() -> None:
    rules = [
        _rule(TrustState.TRUSTED, rid="r1", category="dining"),
        _rule(TrustState.TRUSTED, rid="r2", category="coffee"),
    ]
    out = evaluate_gate(_snapshot(), rules, AutoActionCounters())
    assert out.verdict is GateVerdict.ASK


def test_floor_overrides_a_trusted_rule() -> None:
    # Over the cautious ceiling, even a single trusted rule must ASK.
    out = evaluate_gate(
        _snapshot(amount=Money.from_currency("-200")),
        [_rule(TrustState.TRUSTED)],
        AutoActionCounters(),
    )
    assert out.verdict is GateVerdict.ASK
    assert "floor" in out.reason


def test_build_auto_decision() -> None:
    rule = _rule(TrustState.TRUSTED)
    decision = build_auto_decision(rule, _snapshot(), _EPOCH)
    assert decision.approved
    assert decision.decided_by is DecidedBy.AGENT
    assert decision.rule_id == "r1"
    assert isinstance(decision.allocation, ResolvedCategory)
    assert decision.allocation.category == "dining"
