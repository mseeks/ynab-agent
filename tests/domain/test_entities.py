"""Tests for domain entities and their construction invariants."""

from __future__ import annotations

import datetime

import pytest
from pydantic import ValidationError

from ynab_agent.domain.allocations import (
    ResolvedCategory,
    ResolvedSplit,
    ResolvedSplitLine,
)
from ynab_agent.domain.enums import ClearedState, DecidedBy
from ynab_agent.domain.ids import (
    AccountId,
    CategoryId,
    ReceiptId,
    YnabTransactionId,
)
from ynab_agent.domain.money import Money
from ynab_agent.domain.proposal import Decision
from ynab_agent.domain.receipt import Receipt
from ynab_agent.domain.rule import AmountRange, RuleMatch
from ynab_agent.domain.transaction import (
    Applied,
    Archived,
    AutoApplied,
    Open,
    TxnCore,
    YnabSnapshot,
)

_EPOCH = datetime.datetime(2026, 5, 28, tzinfo=datetime.UTC)


def _snapshot(**kw: object) -> YnabSnapshot:
    base: dict[str, object] = {
        "ynab_id": YnabTransactionId("t1"),
        "account": AccountId("a1"),
        "payee": "Blue Bottle",
        "amount": Money.from_currency("-4.50"),
        "txn_date": datetime.date(2026, 5, 28),
    }
    base.update(kw)
    return YnabSnapshot.model_validate(base)


def _decision(by: DecidedBy, *, approved: bool = True) -> Decision:
    return Decision(
        allocation=ResolvedCategory(category=CategoryId("dining")),
        approved=approved,
        decided_by=by,
        decided_at=_EPOCH,
    )


def _split_decision(a: Money, b: Money) -> Decision:
    return Decision(
        allocation=ResolvedSplit(
            lines=(
                ResolvedSplitLine(category=CategoryId("x"), amount=a),
                ResolvedSplitLine(category=CategoryId("y"), amount=b),
            )
        ),
        approved=True,
        decided_by=DecidedBy.HUMAN,
        decided_at=_EPOCH,
    )


def test_reconciled_is_derived_from_cleared() -> None:
    assert _snapshot(cleared=ClearedState.RECONCILED).reconciled
    assert not _snapshot(cleared=ClearedState.CLEARED).reconciled


def test_categorized_requires_a_category() -> None:
    assert not _snapshot().categorized
    assert _snapshot(category_id=CategoryId("c1")).categorized


def test_has_memo_ignores_blank() -> None:
    assert not _snapshot(memo="   ").has_memo
    assert _snapshot(memo="AmazonBasics cable").has_memo


def test_amount_range_rejects_inverted_bounds() -> None:
    with pytest.raises(ValidationError):
        AmountRange(low=Money.from_currency(100), high=Money.from_currency(10))


def test_amount_range_contains() -> None:
    band = AmountRange(
        low=Money.from_currency(10), high=Money.from_currency(20)
    )
    assert band.contains(Money.from_currency(15))
    assert not band.contains(Money.from_currency(5))
    assert not band.contains(Money.from_currency(25))


def test_open_band_contains() -> None:
    band = AmountRange(low=Money.from_currency(10))
    assert band.contains(Money.from_currency(1_000))
    assert not band.contains(Money.from_currency(5))


def test_rule_match_minimal() -> None:
    match = RuleMatch(payee_pattern="Costco")
    assert match.account is None


def test_receipt_defaults_to_parked() -> None:
    receipt = Receipt(id=ReceiptId("r1"), parked_at=_EPOCH)
    assert receipt.status.value == "parked"
    assert receipt.line_items == ()


def test_auto_applied_requires_agent_decision() -> None:
    core = TxnCore(snapshot=_snapshot())
    with pytest.raises(ValidationError):
        AutoApplied(core=core, decision=_decision(DecidedBy.HUMAN))


def test_open_accepts_either_decider() -> None:
    core = TxnCore(snapshot=_snapshot())
    agent = Open(core=core, decision=_decision(DecidedBy.AGENT))
    human = Open(core=core, decision=_decision(DecidedBy.HUMAN))
    assert agent.decision.decided_by is DecidedBy.AGENT
    assert human.decision.decided_by is DecidedBy.HUMAN


def test_post_write_states_require_approved() -> None:
    core = TxnCore(snapshot=_snapshot())
    with pytest.raises(ValidationError):
        AutoApplied(
            core=core, decision=_decision(DecidedBy.AGENT, approved=False)
        )
    with pytest.raises(ValidationError):
        Applied(core=core, decision=_decision(DecidedBy.HUMAN, approved=False))
    with pytest.raises(ValidationError):
        Open(core=core, decision=_decision(DecidedBy.HUMAN, approved=False))


def test_applied_rejects_unbalanced_split() -> None:
    # snapshot amount is -$4.50; this split sums to -$5.00.
    core = TxnCore(snapshot=_snapshot())
    bad = _split_decision(
        Money.from_currency("-3.00"), Money.from_currency("-2.00")
    )
    with pytest.raises(ValidationError):
        Applied(core=core, decision=bad)


def test_applied_accepts_balanced_split() -> None:
    core = TxnCore(snapshot=_snapshot())  # -$4.50
    good = _split_decision(
        Money.from_currency("-3.00"), Money.from_currency("-1.50")
    )
    assert Applied(core=core, decision=good).decision.approved


def test_archived_requires_reconciled() -> None:
    core = TxnCore(snapshot=_snapshot())  # uncleared
    with pytest.raises(ValidationError):
        Archived(core=core, final=_decision(DecidedBy.HUMAN))


def test_archived_unapplied_requires_categorized() -> None:
    # Reconciled but no category, and never applied (final=None).
    core = TxnCore(snapshot=_snapshot(cleared=ClearedState.RECONCILED))
    with pytest.raises(ValidationError):
        Archived(core=core, final=None)


def test_archived_valid_when_reconciled_and_categorized() -> None:
    core = TxnCore(
        snapshot=_snapshot(
            cleared=ClearedState.RECONCILED, category_id=CategoryId("dining")
        )
    )
    assert Archived(core=core, final=None).final is None
