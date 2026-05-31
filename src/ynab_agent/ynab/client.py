"""The YNAB REST client: read snapshots, commit decisions, read budgets (§1).

A thin, deterministic boundary. The pure mappings (``to_snapshot``,
``to_category_spend``, ``to_patch``) turn the wire format into the strict domain
and a decision into a YNAB patch — they carry the sign/field conventions and are
unit-tested against canned wire data. ``YnabClient`` wires them over a
``YnabBackend`` protocol, so tests inject a fake and strict mypy never reasons
about httpx. ``YnabClient.from_env`` builds the real httpx adapter, reading
``YNAB_API_KEY`` (and optional ``YNAB_BUDGET_ID``); the token never lives here.
"""

from __future__ import annotations

import datetime
import os
from typing import TYPE_CHECKING, Protocol

from ynab_agent.budget.overspend import CategorySpend
from ynab_agent.domain.allocations import ResolvedCategory
from ynab_agent.domain.enums import ClearedState, FlagColor
from ynab_agent.domain.ids import (
    AccountId,
    CategoryId,
    ImportId,
    PayeeId,
    YnabTransactionId,
)
from ynab_agent.domain.money import Money
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.policy.converge import TargetState
from ynab_agent.ynab.wire import WireCategory, WireTransaction

if TYPE_CHECKING:
    import httpx

    from ynab_agent.domain.proposal import Decision

_API_KEY_ENV = "YNAB_API_KEY"
_BUDGET_ENV = "YNAB_BUDGET_ID"
_DEFAULT_BUDGET = "last-used"
_BASE_URL = "https://api.ynab.com/v1"


def to_snapshot(wire: WireTransaction) -> YnabSnapshot:
    """Map a YNAB transaction onto the domain snapshot (SPEC §1)."""
    return YnabSnapshot(
        ynab_id=YnabTransactionId(wire.id),
        account=AccountId(wire.account_id),
        payee=wire.payee_name or "",
        payee_id=PayeeId(wire.payee_id) if wire.payee_id else None,
        amount=Money.from_milliunits(wire.amount),
        txn_date=datetime.date.fromisoformat(wire.date),
        memo=wire.memo,
        flag=FlagColor(wire.flag_color) if wire.flag_color else None,
        category_id=CategoryId(wire.category_id) if wire.category_id else None,
        cleared=ClearedState(wire.cleared),
        approved=wire.approved,
        import_id=ImportId(wire.import_id) if wire.import_id else None,
        matched_transaction_id=YnabTransactionId(wire.matched_transaction_id)
        if wire.matched_transaction_id
        else None,
    )


def to_category_spend(wire: WireCategory) -> CategorySpend:
    """Map a YNAB category onto the domain spend figures (SPEC §7)."""
    return CategorySpend(
        category=CategoryId(wire.id),
        name=wire.name,
        budgeted=Money.from_milliunits(wire.budgeted),
        activity=Money.from_milliunits(wire.activity),
        balance=Money.from_milliunits(wire.balance),
    )


def to_target(snapshot: YnabSnapshot) -> TargetState | None:
    """The read-back end-state of a transaction for verification (SPEC §3 r4).

    Returns ``None`` when there is no single category to verify — a split (whose
    subtransactions a snapshot does not detail) or an uncategorized txn — so the
    spine treats it as could-not-confirm rather than a false divergence.
    """
    if snapshot.category_id is None:
        return None
    return TargetState(
        allocation=ResolvedCategory(category=snapshot.category_id),
        memo=snapshot.memo,
        approved=snapshot.approved,
    )


def to_patch(decision: Decision) -> dict[str, object]:
    """Map a committed decision onto a YNAB transaction patch (SPEC §3).

    A whole-category decision sets ``category_id``; a split sets a null category
    and the subtransactions (YNAB's split shape). ``approved`` and an optional
    memo ride along.
    """
    allocation = decision.allocation
    fields: dict[str, object] = {"approved": decision.approved}
    if decision.memo is not None:
        fields["memo"] = decision.memo
    if isinstance(allocation, ResolvedCategory):
        fields["category_id"] = str(allocation.category)
    else:
        fields["category_id"] = None
        fields["subtransactions"] = [
            {
                "amount": line.amount.milliunits,
                "category_id": str(line.category),
                "memo": line.memo,
            }
            for line in allocation.lines
        ]
    return fields


class YnabBackend(Protocol):
    """The YNAB operations the client needs (implemented over httpx)."""

    def get_transaction(self, txn_id: str) -> WireTransaction:
        """Fetch one transaction."""
        ...

    def patch_transaction(self, txn_id: str, fields: dict[str, object]) -> None:
        """Patch one transaction with the given fields."""
        ...

    def list_categories(self) -> tuple[WireCategory, ...]:
        """List the budget's live categories."""
        ...


class YnabClient:
    """Reads and writes YNAB through a backend, in domain terms (SPEC §1)."""

    def __init__(self, backend: YnabBackend) -> None:
        """Wrap a backend (the real httpx adapter, or a test fake)."""
        self._backend = backend

    @classmethod
    def from_env(cls) -> YnabClient:
        """Build a client backed by the real YNAB REST API (reads the key).

        Raises:
            RuntimeError: If ``YNAB_API_KEY`` is not set.
        """
        token = os.environ.get(_API_KEY_ENV)
        if not token:
            msg = f"{_API_KEY_ENV} is not set"
            raise RuntimeError(msg)
        budget = os.environ.get(_BUDGET_ENV, _DEFAULT_BUDGET)
        import httpx

        client = httpx.Client(
            base_url=_BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        from ynab_agent.telemetry import instrument_httpx

        instrument_httpx(client)
        return cls(_HttpxBackend(client, budget))

    def snapshot(self, txn_id: str) -> YnabSnapshot | None:
        """The current snapshot of a transaction, or ``None`` if deleted."""
        wire = self._backend.get_transaction(txn_id)
        if wire.deleted:
            return None
        return to_snapshot(wire)

    def category_spends(self) -> tuple[CategorySpend, ...]:
        """Every live category's month-to-date budget figures (SPEC §7)."""
        return tuple(
            to_category_spend(c) for c in self._backend.list_categories()
        )

    def commit(self, txn_id: str, decision: Decision) -> None:
        """Commit a decision to a transaction (SPEC §3)."""
        self._backend.patch_transaction(txn_id, to_patch(decision))

    def read_back(self, txn_id: str) -> TargetState | None:
        """Re-read a transaction's end-state for verification (SPEC §3 r4)."""
        snapshot = self.snapshot(txn_id)
        return to_target(snapshot) if snapshot is not None else None


class _HttpxBackend:
    """Adapts the YNAB REST API to the :class:`YnabBackend` protocol."""

    def __init__(self, client: httpx.Client, budget_id: str) -> None:
        self._client = client
        self._budget = budget_id

    def get_transaction(self, txn_id: str) -> WireTransaction:
        path = f"/budgets/{self._budget}/transactions/{txn_id}"
        response = self._client.get(path)
        response.raise_for_status()
        return WireTransaction.model_validate(
            response.json()["data"]["transaction"]
        )

    def patch_transaction(self, txn_id: str, fields: dict[str, object]) -> None:
        path = f"/budgets/{self._budget}/transactions/{txn_id}"
        response = self._client.patch(path, json={"transaction": fields})
        response.raise_for_status()

    def list_categories(self) -> tuple[WireCategory, ...]:
        path = f"/budgets/{self._budget}/categories"
        response = self._client.get(path)
        response.raise_for_status()
        groups = response.json()["data"]["category_groups"]
        return tuple(
            WireCategory.model_validate(category)
            for group in groups
            for category in group["categories"]
            if not category.get("deleted") and not category.get("hidden")
        )
