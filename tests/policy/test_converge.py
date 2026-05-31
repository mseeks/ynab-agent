"""Tests for the converge-to-target reconciliation (SPEC §3 rules 2-4)."""

from __future__ import annotations

import datetime

from ynab_agent.domain.allocations import ResolvedCategory
from ynab_agent.domain.enums import ClearedState, DecidedBy
from ynab_agent.domain.events import VerifyOutcome
from ynab_agent.domain.ids import AccountId, CategoryId, YnabTransactionId
from ynab_agent.domain.money import Money
from ynab_agent.domain.proposal import Decision
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.policy.converge import (
    TargetState,
    classify_verify,
    content_hash,
    needs_write,
    reconciliation_blocks,
    target_of,
)

_EPOCH = datetime.datetime(2026, 5, 28, tzinfo=datetime.UTC)


def _target(category: str = "dining", memo: str | None = None) -> TargetState:
    return TargetState(
        allocation=ResolvedCategory(category=CategoryId(category)), memo=memo
    )


def test_content_hash_is_stable_and_distinguishing() -> None:
    assert content_hash(_target()) == content_hash(_target())
    assert content_hash(_target("dining")) != content_hash(_target("gifts"))
    assert content_hash(_target(memo="a")) != content_hash(_target(memo="b"))


def test_needs_write_skips_an_equal_state() -> None:
    assert not needs_write(_target(), _target())
    assert needs_write(_target("dining"), _target("gifts"))
    assert needs_write(None, _target())


def test_classify_verify_outcomes() -> None:
    target = _target("dining")
    assert classify_verify(target, target) is VerifyOutcome.MATCH
    assert classify_verify(None, target) is VerifyOutcome.COULD_NOT_CONFIRM
    assert classify_verify(_target("gifts"), target) is VerifyOutcome.DIVERGED


def test_target_of_projects_a_decision() -> None:
    decision = Decision(
        allocation=ResolvedCategory(category=CategoryId("dining")),
        memo="coffee",
        approved=True,
        decided_by=DecidedBy.AGENT,
        decided_at=_EPOCH,
    )
    target = target_of(decision)
    assert target.memo == "coffee"
    assert content_hash(target) == content_hash(_target(memo="coffee"))


def test_reconciliation_guard() -> None:
    base: dict[str, object] = {
        "ynab_id": YnabTransactionId("t1"),
        "account": AccountId("a1"),
        "payee": "Blue Bottle",
        "amount": Money.from_currency("-4.50"),
        "txn_date": datetime.date(2026, 5, 28),
    }
    reconciled = YnabSnapshot.model_validate(
        {**base, "cleared": ClearedState.RECONCILED}
    )
    closed_month = YnabSnapshot.model_validate({**base, "month_closed": True})
    cleared = YnabSnapshot.model_validate(
        {**base, "cleared": ClearedState.CLEARED}
    )
    assert reconciliation_blocks(reconciled)
    assert reconciliation_blocks(closed_month)
    assert not reconciliation_blocks(cleared)
