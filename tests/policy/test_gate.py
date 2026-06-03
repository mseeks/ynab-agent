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
    matching_rules,
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
    source: RuleSource = RuleSource.LEARNED,
) -> Rule:
    return Rule(
        id=RuleId(rid),
        match=match or RuleMatch(payee_pattern=payee),
        action=RuleAction(
            allocation=ProposedCategory(category=CategoryId(category))
        ),
        trust=trust,
        source=source,
    )


# A trusted, human-blessed rule is the only kind the gate auto-applies (§14).
_BLESSED = RuleSource.HUMAN_EXPLICIT


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


# matching_rules — the filter that decides which rules reach the AUTO path.
# A false match here auto-applies a wrong category with no human review, so the
# AND logic and the None-memo guard are tested directly (test-backfill #1).
def test_matching_rules_returns_matches() -> None:
    rule = _rule(TrustState.SUGGESTED, payee="Blue Bottle")
    assert matching_rules([rule], _snapshot()) == [rule]


def test_matching_rules_excludes_payee_miss() -> None:
    rule = _rule(TrustState.SUGGESTED, payee="Amazon")
    assert matching_rules([rule], _snapshot(payee="Whole Foods")) == []


def test_matching_rules_none_memo_with_item_keyword_is_excluded() -> None:
    band = RuleMatch(payee_pattern="Blue Bottle", item_keyword="prime")
    rule = _rule(TrustState.SUGGESTED, match=band)
    # snapshot.memo is None → `memo or ""` guards against a crash and a match.
    assert matching_rules([rule], _snapshot(memo=None)) == []


def test_matching_rules_empty_input_returns_empty() -> None:
    assert matching_rules([], _snapshot()) == []


def test_single_blessed_rule_is_auto() -> None:
    out = evaluate_gate(
        _snapshot(),
        [_rule(TrustState.TRUSTED, source=_BLESSED)],
        AutoActionCounters(),
    )
    assert out.verdict is GateVerdict.AUTO
    assert out.rule_id == "r1"


def test_trusted_but_unblessed_rule_asks() -> None:
    # Reaching `trusted` by consistency is eligibility, not autonomy: a learned
    # rule still ASKs until the owner blesses it (§14 opt-in).
    out = evaluate_gate(
        _snapshot(), [_rule(TrustState.TRUSTED)], AutoActionCounters()
    )
    assert out.verdict is GateVerdict.ASK
    assert "blessed" in out.reason


def test_no_trusted_rule_asks() -> None:
    out = evaluate_gate(
        _snapshot(), [_rule(TrustState.CONFIRMED)], AutoActionCounters()
    )
    assert out.verdict is GateVerdict.ASK


def test_conflicting_blessed_rules_ask() -> None:
    rules = [
        _rule(TrustState.TRUSTED, source=_BLESSED, rid="r1", category="dining"),
        _rule(TrustState.TRUSTED, source=_BLESSED, rid="r2", category="coffee"),
    ]
    out = evaluate_gate(_snapshot(), rules, AutoActionCounters())
    assert out.verdict is GateVerdict.ASK


def test_floor_overrides_a_blessed_rule() -> None:
    # Over the cautious ceiling, even a single blessed rule must ASK.
    out = evaluate_gate(
        _snapshot(amount=Money.from_currency("-200")),
        [_rule(TrustState.TRUSTED, source=_BLESSED)],
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
