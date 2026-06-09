"""Tests for the YNAB client mappings and backend wiring (SPEC §1, §3, §7).

The pure mappings are tested against canned wire data; the client is tested over
a fake backend. One opt-in live test (skipped unless ``YNAB_API_KEY`` is set)
hits the real API.
"""

from __future__ import annotations

import datetime
import os

import pytest

from ynab_agent.domain.allocations import (
    ResolvedCategory,
    ResolvedSplit,
    ResolvedSplitLine,
)
from ynab_agent.domain.enums import ClearedState, DecidedBy
from ynab_agent.domain.events import VerifyOutcome
from ynab_agent.domain.ids import CategoryId, RuleId
from ynab_agent.domain.money import Money
from ynab_agent.domain.proposal import Decision
from ynab_agent.policy.converge import classify_verify, target_of
from ynab_agent.ynab.client import (
    AGENT_REVIEW_FLAG,
    YnabClient,
    to_category_spend,
    to_patch,
    to_snapshot,
    to_target,
)
from ynab_agent.ynab.wire import WireCategory, WireMonth, WireTransaction

_NOW = datetime.datetime(2026, 5, 31, 12, 0, tzinfo=datetime.UTC)


def _wire_txn(**kw: object) -> WireTransaction:
    base: dict[str, object] = {
        "id": "t1",
        "account_id": "a1",
        "date": "2026-05-28",
        "amount": -4500,
        "approved": False,
        "payee_name": "Blue Bottle Coffee",
        "cleared": "reconciled",
        "category_id": "dining",
    }
    base.update(kw)
    return WireTransaction.model_validate(base)


def test_to_snapshot_maps_core_fields() -> None:
    snap = to_snapshot(_wire_txn())
    assert snap.ynab_id == "t1"
    assert snap.payee == "Blue Bottle Coffee"
    assert snap.amount == Money.from_currency("-4.50")
    assert snap.txn_date == datetime.date(2026, 5, 28)
    assert snap.cleared is ClearedState.RECONCILED
    assert snap.reconciled is True
    assert snap.category_id == "dining"


def test_to_snapshot_handles_nulls() -> None:
    snap = to_snapshot(
        _wire_txn(payee_name=None, category_id=None, cleared="uncleared")
    )
    assert snap.payee == ""
    assert snap.category_id is None
    assert snap.cleared is ClearedState.UNCLEARED


def test_to_category_spend_maps_figures() -> None:
    spend = to_category_spend(
        WireCategory(
            id="dining",
            name="Dining Out",
            budgeted=400000,
            activity=-210000,
            balance=190000,
        )
    )
    assert spend.category == "dining"
    assert spend.budgeted == Money.from_currency("400")
    assert spend.activity == Money.from_currency("-210")


def test_to_patch_for_a_category_decision() -> None:
    decision = Decision(
        allocation=ResolvedCategory(category=CategoryId("dining")),
        memo="coffee",
        approved=True,
        decided_by=DecidedBy.HUMAN,
        decided_at=_NOW,
    )
    patch = to_patch(decision)
    assert patch == {
        "approved": True,
        "memo": "coffee",
        "category_id": "dining",
    }
    # A human decision leaves the flag untouched (no auto-action marker).
    assert "flag_color" not in patch


def test_to_patch_agent_decision_is_flagged_for_review() -> None:
    # An agent-applied write carries the review flag so it surfaces in the YNAB
    # app for the owner to clear as implicit review (SPEC §14.5).
    decision = Decision(
        allocation=ResolvedCategory(category=CategoryId("subscriptions")),
        approved=True,
        decided_by=DecidedBy.AGENT,
        decided_at=_NOW,
        rule_id=RuleId("r1"),
    )
    patch = to_patch(decision)
    assert patch["flag_color"] == AGENT_REVIEW_FLAG.value


def test_to_patch_for_a_split_decision() -> None:
    decision = Decision(
        allocation=ResolvedSplit(
            lines=(
                ResolvedSplitLine(
                    category=CategoryId("groceries"),
                    amount=Money.from_milliunits(-6000),
                ),
                ResolvedSplitLine(
                    category=CategoryId("household"),
                    amount=Money.from_milliunits(-4000),
                ),
            )
        ),
        approved=True,
        decided_by=DecidedBy.HUMAN,
        decided_at=_NOW,
    )
    patch = to_patch(decision)
    assert patch["category_id"] is None
    assert patch["subtransactions"] == [
        {"amount": -6000, "category_id": "groceries", "memo": None},
        {"amount": -4000, "category_id": "household", "memo": None},
    ]


class _FakeBackend:
    def __init__(
        self,
        txn: WireTransaction,
        *,
        delta: tuple[tuple[WireTransaction, ...], int] = ((), 0),
        unapproved: tuple[WireTransaction, ...] = (),
        get_returns_none: bool = False,
        to_be_budgeted: int = 0,
        month_categories: dict[str, WireCategory] | None = None,
    ) -> None:
        self._txn = txn
        self._delta = delta
        self._unapproved = unapproved
        self._get_returns_none = get_returns_none
        self._to_be_budgeted = to_be_budgeted
        self._month_categories = month_categories or {}
        self.patched: list[tuple[str, dict[str, object]]] = []
        self.delta_calls: list[tuple[str, int | None]] = []
        self.budget_sets: list[tuple[str, str, int]] = []

    def get_transaction(self, txn_id: str) -> WireTransaction | None:
        return None if self._get_returns_none else self._txn

    def patch_transaction(self, txn_id: str, fields: dict[str, object]) -> None:
        self.patched.append((txn_id, fields))

    def list_categories(self) -> tuple[WireCategory, ...]:
        return ()

    def list_transactions(
        self, since_date: str, server_knowledge: int | None
    ) -> tuple[tuple[WireTransaction, ...], int]:
        self.delta_calls.append((since_date, server_knowledge))
        return self._delta

    def list_unapproved(self) -> tuple[WireTransaction, ...]:
        return self._unapproved

    def get_month(self, month: str) -> WireMonth:
        return WireMonth(month=month, to_be_budgeted=self._to_be_budgeted)

    def get_month_category(
        self, month: str, category_id: str
    ) -> WireCategory | None:
        return self._month_categories.get(category_id)

    def set_category_budgeted(
        self, month: str, category_id: str, budgeted_milliunits: int
    ) -> None:
        self.budget_sets.append((month, category_id, budgeted_milliunits))


def test_client_snapshot_maps_through_the_backend() -> None:
    client = YnabClient(_FakeBackend(_wire_txn()))
    snap = client.snapshot("t1")
    assert snap is not None
    assert snap.payee == "Blue Bottle Coffee"


def test_client_snapshot_is_none_when_deleted() -> None:
    client = YnabClient(_FakeBackend(_wire_txn(deleted=True)))
    assert client.snapshot("t1") is None


def test_client_snapshot_falls_back_to_list_for_unapproved() -> None:
    # YNAB's single GET 404s (None) for unapproved txns; snapshot finds it in
    # the transactions list instead.
    wire = _wire_txn(approved=False)
    backend = _FakeBackend(wire, get_returns_none=True, delta=((wire,), 5))
    snap = YnabClient(backend).snapshot("t1")
    assert snap is not None
    assert snap.ynab_id == "t1"
    # The fallback list is the delta-from-zero (cursor 0), which is the only
    # form that surfaces matched/scheduled unapproved transactions.
    assert backend.delta_calls and backend.delta_calls[0][1] == 0


def test_client_snapshot_none_when_missing_everywhere() -> None:
    # 404 on the single GET and absent from the list → genuinely gone.
    backend = _FakeBackend(_wire_txn(), get_returns_none=True, delta=((), 0))
    assert YnabClient(backend).snapshot("t1") is None


def test_to_target_maps_a_categorized_snapshot() -> None:
    target = to_target(to_snapshot(_wire_txn(memo="lunch")))
    assert target is not None
    assert isinstance(target.allocation, ResolvedCategory)
    assert target.allocation.category == "dining"
    assert target.memo == "lunch"


def test_to_target_is_none_for_an_uncategorized_snapshot() -> None:
    # No category and no subtransactions to verify → could-not-confirm.
    assert to_target(to_snapshot(_wire_txn(category_id=None))) is None


def test_to_snapshot_maps_split_subtransactions() -> None:
    snap = to_snapshot(
        _wire_txn(
            category_id=None,
            subtransactions=[
                {"amount": -3000, "category_id": "groceries", "memo": "food"},
                {"amount": -1500, "category_id": "gifts"},
                # deleted and uncategorized lines are dropped.
                {"amount": 0, "category_id": "x", "deleted": True},
                {"amount": -500, "category_id": None},
            ],
        )
    )
    assert {str(line.category) for line in snap.subtransactions} == {
        "groceries",
        "gifts",
    }


def test_to_target_reconstructs_a_split() -> None:
    # A split parent (null category) now verifies field-by-field from its
    # subtransactions, rather than always reading as could-not-confirm.
    target = to_target(
        to_snapshot(
            _wire_txn(
                category_id=None,
                subtransactions=[
                    {"amount": -3000, "category_id": "groceries"},
                    {"amount": -1500, "category_id": "gifts"},
                ],
            )
        )
    )
    assert target is not None
    assert isinstance(target.allocation, ResolvedSplit)
    assert {str(line.category) for line in target.allocation.lines} == {
        "groceries",
        "gifts",
    }


def test_split_write_verifies_match_even_if_lines_reorder() -> None:
    # The end-to-end split bug: a written split must MATCH on read-back. YNAB
    # may return the subtransactions in a different order, so the verify is
    # order-insensitive (SPEC §3 r4).
    decision = Decision(
        allocation=ResolvedSplit(
            lines=(
                ResolvedSplitLine(
                    category=CategoryId("gifts"),
                    amount=Money.from_milliunits(-1500),
                    memo="present",
                ),
                ResolvedSplitLine(
                    category=CategoryId("groceries"),
                    amount=Money.from_milliunits(-3000),
                    memo="food",
                ),
            )
        ),
        approved=True,
        decided_by=DecidedBy.HUMAN,
        decided_at=_NOW,
    )
    # Read-back returns the lines in the opposite order, with the parent
    # category nulled (YNAB's split shape).
    read = to_target(
        to_snapshot(
            _wire_txn(
                approved=True,  # YNAB shows it approved after the write
                category_id=None,
                subtransactions=[
                    {
                        "amount": -3000,
                        "category_id": "groceries",
                        "memo": "food",
                    },
                    {
                        "amount": -1500,
                        "category_id": "gifts",
                        "memo": "present",
                    },
                ],
            )
        )
    )
    assert classify_verify(read, target_of(decision)) is VerifyOutcome.MATCH


def test_client_read_back_round_trips_to_a_target() -> None:
    target = YnabClient(_FakeBackend(_wire_txn())).read_back("t1")
    assert target is not None
    assert isinstance(target.allocation, ResolvedCategory)
    assert target.allocation.category == "dining"


def test_client_commit_patches_the_transaction() -> None:
    backend = _FakeBackend(_wire_txn())
    decision = Decision(
        allocation=ResolvedCategory(category=CategoryId("dining")),
        approved=True,
        decided_by=DecidedBy.AGENT,
        decided_at=_NOW,
    )
    YnabClient(backend).commit("t1", decision)
    assert backend.patched[0][0] == "t1"
    assert backend.patched[0][1]["category_id"] == "dining"


def test_client_unapproved_maps_snapshots_and_drops_deleted() -> None:
    backend = _FakeBackend(
        _wire_txn(),
        unapproved=(_wire_txn(id="t1"), _wire_txn(id="t2", deleted=True)),
    )
    snapshots = YnabClient(backend).unapproved()
    assert [s.ynab_id for s in snapshots] == ["t1"]  # deleted t2 dropped


def test_client_unapproved_is_empty_when_none() -> None:
    backend = _FakeBackend(_wire_txn(), unapproved=())
    assert YnabClient(backend).unapproved() == ()


def test_client_ready_to_assign_reads_to_be_budgeted() -> None:
    backend = _FakeBackend(_wire_txn(), to_be_budgeted=125000)
    assert YnabClient(backend).ready_to_assign() == Money.from_currency("125")


def test_client_set_budgeted_writes_an_absolute_value() -> None:
    backend = _FakeBackend(_wire_txn())
    YnabClient(backend).set_budgeted("dining", Money.from_currency("520"))
    # An absolute milliunit write to the current month, idempotent on retry.
    assert backend.budget_sets == [("current", "dining", 520000)]


def test_client_read_budgeted_round_trips() -> None:
    cat = WireCategory(
        id="dining", name="Dining", budgeted=520000, activity=0, balance=520000
    )
    backend = _FakeBackend(_wire_txn(), month_categories={"dining": cat})
    assert YnabClient(backend).read_budgeted("dining") == Money.from_currency(
        "520"
    )


def test_client_read_budgeted_is_none_when_unread() -> None:
    backend = _FakeBackend(_wire_txn(), month_categories={})
    assert YnabClient(backend).read_budgeted("dining") is None


@pytest.mark.skipif(
    not os.environ.get("YNAB_API_KEY"),
    reason="set YNAB_API_KEY to run the live YNAB smoke",
)
def test_live_ynab_lists_categories() -> None:
    spends = YnabClient.from_env().category_spends()
    assert spends  # a real budget has categories
