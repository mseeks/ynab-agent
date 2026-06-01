"""Tests for the W1 ingestion planning core (SPEC §2, §13)."""

from __future__ import annotations

import datetime

from ynab_agent.domain.ids import AccountId, YnabTransactionId
from ynab_agent.domain.money import Money
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.ingest.plan import is_duplicate_import, plan_ingest
from ynab_agent.ingest.scope import IngestScope, in_scope

_INSTALL = datetime.date(2026, 5, 1)


def _snapshot(
    *,
    ynab_id: str = "t1",
    account: str = "a1",
    day: int = 15,
    matched: str | None = None,
) -> YnabSnapshot:
    return YnabSnapshot(
        ynab_id=YnabTransactionId(ynab_id),
        account=AccountId(account),
        payee="Blue Bottle",
        amount=Money.from_currency("-4.50"),
        txn_date=datetime.date(2026, 5, day),
        matched_transaction_id=(
            YnabTransactionId(matched) if matched is not None else None
        ),
    )


def _scope(*, accounts: set[str] | None = None) -> IngestScope:
    return IngestScope(
        budget_id="b1",
        install_date=_INSTALL,
        account_ids=(
            frozenset(AccountId(a) for a in accounts)
            if accounts is not None
            else None
        ),
    )


def test_in_scope_excludes_pre_install_dates() -> None:
    pre = _snapshot(day=15).model_copy(
        update={"txn_date": datetime.date(2026, 4, 30)}
    )
    assert not in_scope(pre, _scope())
    assert in_scope(_snapshot(day=15), _scope())


def test_in_scope_respects_account_subset() -> None:
    scope = _scope(accounts={"a1"})
    assert in_scope(_snapshot(account="a1"), scope)
    assert not in_scope(_snapshot(account="a2"), scope)


def test_in_scope_all_accounts_when_none() -> None:
    assert in_scope(_snapshot(account="anything"), _scope())


def test_is_duplicate_import() -> None:
    assert is_duplicate_import(_snapshot(matched="t9"))
    assert not is_duplicate_import(_snapshot())


def test_cold_start_addresses_nothing() -> None:
    plan = plan_ingest([_snapshot()], _scope(), cold_start=True)
    assert plan == ()


def test_plan_addresses_in_scope_only() -> None:
    snaps = [
        _snapshot(ynab_id="t1", account="a1"),
        _snapshot(ynab_id="t2", account="a2"),  # out of account scope
    ]
    plan = plan_ingest(snaps, _scope(accounts={"a1"}), cold_start=False)
    assert [a.snapshot.ynab_id for a in plan] == ["t1"]


def test_plan_routes_duplicate_imports_to_human() -> None:
    plan = plan_ingest([_snapshot(matched="t9")], _scope(), cold_start=False)
    assert len(plan) == 1
    assert plan[0].route_to_human


def test_plan_skips_already_approved_transactions() -> None:
    # An approved transaction is settled — never re-triaged (SPEC §2/§13). Only
    # the outstanding, unapproved one is addressed.
    approved = _snapshot(ynab_id="t1").model_copy(update={"approved": True})
    unapproved = _snapshot(ynab_id="t2")
    plan = plan_ingest([approved, unapproved], _scope(), cold_start=False)
    assert [a.snapshot.ynab_id for a in plan] == ["t2"]
