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
from ynab_agent.domain.allocations import (
    ResolvedCategory,
    ResolvedSplit,
    ResolvedSplitLine,
)
from ynab_agent.domain.enums import ClearedState, DecidedBy, FlagColor
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
from ynab_agent.ynab.wire import (
    WireCategory,
    WireMonth,
    WireSubtransaction,
    WireTransaction,
)

if TYPE_CHECKING:
    import httpx

    from ynab_agent.domain.proposal import Decision

_API_KEY_ENV = "YNAB_API_KEY"
_BUDGET_ENV = "YNAB_BUDGET_ID"
_DEFAULT_BUDGET = "last-used"
_BASE_URL = "https://api.ynab.com/v1"
# The month identifier the balancer operates on (SPEC §8): YNAB's "current"
# shorthand for the live budget month, matching the W6 monitor's current-month
# figures. ``/months/{month}/...`` also accepts ``YYYY-MM-01`` for later use.
CURRENT_MONTH = "current"
# How far back the snapshot fallback list scans when the single GET 404s (an
# unapproved txn). The agent only reads recent transactions, so a year is ample.
_FALLBACK_LOOKBACK_DAYS = 365
_HTTP_NOT_FOUND = 404
# The flag an agent-applied write carries so the owner sees it in the YNAB app
# and can clear it as implicit review of an auto-action (SPEC §14.5).
AGENT_REVIEW_FLAG = FlagColor.PURPLE


def _to_split_lines(
    subs: tuple[WireSubtransaction, ...],
) -> tuple[ResolvedSplitLine, ...]:
    """Map a split's wire subtransactions onto domain split lines (SPEC §3).

    Skips deleted lines and any line without a category (an uncategorized line
    cannot be a :class:`ResolvedSplitLine`); the agent only writes fully
    categorized splits, so a clean read-back maps every line.
    """
    return tuple(
        ResolvedSplitLine(
            category=CategoryId(sub.category_id),
            amount=Money.from_milliunits(sub.amount),
            memo=sub.memo,
        )
        for sub in subs
        if not sub.deleted and sub.category_id
    )


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
        subtransactions=_to_split_lines(wire.subtransactions),
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

    Reconstructs a split from its subtransactions so a split write verifies
    field-by-field (a clean read-back hashes equal to the target); falls back to
    a whole-category target; returns ``None`` only for an uncategorized txn (no
    end-state to confirm), so the spine treats that as could-not-confirm rather
    than a false divergence.
    """
    if len(snapshot.subtransactions) > 1:  # a split has at least two lines
        return TargetState(
            allocation=ResolvedSplit(lines=snapshot.subtransactions),
            memo=snapshot.memo,
            approved=snapshot.approved,
        )
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
    # Flag an agent-applied write so it surfaces in the YNAB app for review
    # (SPEC §14.5); a human decision leaves the existing flag untouched.
    if decision.decided_by is DecidedBy.AGENT:
        fields["flag_color"] = AGENT_REVIEW_FLAG.value
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

    def get_transaction(self, txn_id: str) -> WireTransaction | None:
        """Fetch one transaction, or ``None`` if the GET 404s (unapproved)."""
        ...

    def patch_transaction(self, txn_id: str, fields: dict[str, object]) -> None:
        """Patch one transaction with the given fields."""
        ...

    def list_categories(self) -> tuple[WireCategory, ...]:
        """List the budget's live categories."""
        ...

    def list_transactions(
        self, since_date: str, server_knowledge: int | None
    ) -> tuple[tuple[WireTransaction, ...], int]:
        """The transactions delta and the advanced ``server_knowledge``."""
        ...

    def list_unapproved(self) -> tuple[WireTransaction, ...]:
        """The budget's ``type=unapproved`` transactions."""
        ...

    def get_month(self, month: str) -> WireMonth:
        """The budget month, for its ``to_be_budgeted`` (Ready-to-Assign)."""
        ...

    def get_month_category(
        self, month: str, category_id: str
    ) -> WireCategory | None:
        """One category's figures for ``month``, or ``None`` if the GET 404s."""
        ...

    def set_category_budgeted(
        self, month: str, category_id: str, budgeted_milliunits: int
    ) -> None:
        """Set a month category's ``budgeted`` to a milliunit value."""
        ...


# The process-wide cached client built by ``from_env`` (see its docstring).
# ``tests/conftest.py`` resets this between tests.
_CACHED: YnabClient | None = None


class YnabClient:
    """Reads and writes YNAB through a backend, in domain terms (SPEC §1)."""

    def __init__(self, backend: YnabBackend) -> None:
        """Wrap a backend (the real httpx adapter, or a test fake)."""
        self._backend = backend

    @classmethod
    def from_env(cls) -> YnabClient:
        """Build (once) a client backed by the real YNAB REST API.

        The pooled httpx client and its OTel instrumentation are built on first
        use and cached: every activity invocation calls this on the worker's hot
        path, so a fresh-per-call client (never closed) would leak connections +
        file descriptors. Tests reset the cache (see ``tests/conftest.py``).

        Raises:
            RuntimeError: If ``YNAB_API_KEY`` is not set.
        """
        global _CACHED
        if _CACHED is not None:
            return _CACHED
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
        _CACHED = cls(_HttpxBackend(client, budget))
        return _CACHED

    def snapshot(self, txn_id: str) -> YnabSnapshot | None:
        """The current snapshot of a transaction, or ``None`` if not found.

        YNAB's single-transaction GET returns 404 for *unapproved* transactions
        — including matched/scheduled imports — which are exactly the ones the
        agent triages. Those do appear in the transactions list, so on a miss we
        fall back to a bounded list scan (the agent only ever reads recent
        transactions). An approved transaction uses the cheap single GET.
        """
        wire = self._backend.get_transaction(txn_id)
        if wire is None:
            since = (
                datetime.datetime.now(datetime.UTC).date()
                - datetime.timedelta(days=_FALLBACK_LOOKBACK_DAYS)
            ).isoformat()
            # ``last_knowledge_of_server=0`` is required: matched/scheduled
            # unapproved transactions appear only in the delta-from-zero, not a
            # plain since_date list (a YNAB quirk).
            wires, _ = self._backend.list_transactions(since, 0)
            wire = next((w for w in wires if w.id == txn_id), None)
        if wire is None or wire.deleted:
            return None
        return to_snapshot(wire)

    def category_spends(self) -> tuple[CategorySpend, ...]:
        """Every live category's month-to-date budget figures (SPEC §7)."""
        return tuple(
            to_category_spend(c) for c in self._backend.list_categories()
        )

    def unapproved(self) -> tuple[YnabSnapshot, ...]:
        """The budget's currently *unapproved* transactions (SPEC §2 W1).

        This is YNAB's own ``type=unapproved`` view — the transactions awaiting
        the owner's review. It is the agent's outstanding-work set: a
        tentatively scheduled/auto-matched import is *not* in it (YNAB excludes
        it until it is combined), so the agent ignores those until they truly
        land. Deleted rows drop out. The poll re-reads this each tick — the set
        is small, so no cursor is needed (and none is stored, SPEC §0.5).
        """
        return tuple(
            to_snapshot(wire)
            for wire in self._backend.list_unapproved()
            if not wire.deleted
        )

    def recent(self, since: datetime.date) -> tuple[YnabSnapshot, ...]:
        """Every transaction on or after ``since`` (the W4 candidate pool).

        The receipt matcher's read: the delta-from-zero list (the same YNAB
        quirk ``snapshot``'s fallback uses, so unapproved imports are
        included), deleted rows dropped. Bounded by the caller's window — the
        join only ever looks weeks back, never the full history.
        """
        wires, _ = self._backend.list_transactions(since.isoformat(), 0)
        return tuple(to_snapshot(wire) for wire in wires if not wire.deleted)

    def commit(self, txn_id: str, decision: Decision) -> None:
        """Commit a decision to a transaction (SPEC §3)."""
        self._backend.patch_transaction(txn_id, to_patch(decision))

    def patch_memo(self, txn_id: str, memo: str) -> None:
        """Write ONLY the memo (the W4 fold for settled charges, SPEC §6).

        A receipt is detail: folding it into a hand-approved or pre-install
        transaction must not touch the category, the approval, or the flag —
        a partial PATCH leaves every other field exactly as the owner set it.
        """
        self._backend.patch_transaction(txn_id, {"memo": memo})

    def read_back(self, txn_id: str) -> TargetState | None:
        """Re-read a transaction's end-state for verification (SPEC §3 r4)."""
        snapshot = self.snapshot(txn_id)
        return to_target(snapshot) if snapshot is not None else None

    def ready_to_assign(self, month: str = CURRENT_MONTH) -> Money:
        """The month's Ready-to-Assign — the first balancer source (SPEC §8)."""
        return Money.from_milliunits(
            self._backend.get_month(month).to_be_budgeted
        )

    def set_budgeted(
        self, category_id: str, target: Money, month: str = CURRENT_MONTH
    ) -> None:
        """Set a category's month ``budgeted`` to ``target`` (SPEC §8).

        An *absolute* write: the workflow computes each category's new budgeted
        from a snapshot and sets it directly, so an activity retry is idempotent
        (re-setting the same value is a no-op) — unlike a relative add.
        """
        self._backend.set_category_budgeted(
            month, category_id, target.milliunits
        )

    def read_budgeted(
        self, category_id: str, month: str = CURRENT_MONTH
    ) -> Money | None:
        """Re-read a category's month ``budgeted`` for verification (SPEC §8).

        ``None`` when the category can't be read, so the spine treats it as
        could-not-confirm rather than false divergence (cf. :meth:`read_back`).
        """
        wire = self._backend.get_month_category(month, category_id)
        return (
            Money.from_milliunits(wire.budgeted) if wire is not None else None
        )


class _HttpxBackend:
    """Adapts the YNAB REST API to the :class:`YnabBackend` protocol."""

    def __init__(self, client: httpx.Client, budget_id: str) -> None:
        self._client = client
        self._budget = budget_id

    def get_transaction(self, txn_id: str) -> WireTransaction | None:
        path = f"/budgets/{self._budget}/transactions/{txn_id}"
        response = self._client.get(path)
        if response.status_code == _HTTP_NOT_FOUND:
            return None
        response.raise_for_status()
        return WireTransaction.model_validate(
            response.json()["data"]["transaction"]
        )

    def patch_transaction(self, txn_id: str, fields: dict[str, object]) -> None:
        path = f"/budgets/{self._budget}/transactions/{txn_id}"
        response = self._client.patch(path, json={"transaction": fields})
        response.raise_for_status()

    def list_transactions(
        self, since_date: str, server_knowledge: int | None
    ) -> tuple[tuple[WireTransaction, ...], int]:
        path = f"/budgets/{self._budget}/transactions"
        params: dict[str, str | int] = {"since_date": since_date}
        if server_knowledge is not None:
            params["last_knowledge_of_server"] = server_knowledge
        response = self._client.get(path, params=params)
        response.raise_for_status()
        data = response.json()["data"]
        transactions = tuple(
            WireTransaction.model_validate(item)
            for item in data["transactions"]
        )
        return transactions, int(data["server_knowledge"])

    def list_unapproved(self) -> tuple[WireTransaction, ...]:
        path = f"/budgets/{self._budget}/transactions"
        response = self._client.get(path, params={"type": "unapproved"})
        response.raise_for_status()
        return tuple(
            WireTransaction.model_validate(item)
            for item in response.json()["data"]["transactions"]
        )

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

    def get_month(self, month: str) -> WireMonth:
        path = f"/budgets/{self._budget}/months/{month}"
        response = self._client.get(path)
        response.raise_for_status()
        return WireMonth.model_validate(response.json()["data"]["month"])

    def get_month_category(
        self, month: str, category_id: str
    ) -> WireCategory | None:
        path = (
            f"/budgets/{self._budget}/months/{month}/categories/{category_id}"
        )
        response = self._client.get(path)
        if response.status_code == _HTTP_NOT_FOUND:
            return None
        response.raise_for_status()
        return WireCategory.model_validate(response.json()["data"]["category"])

    def set_category_budgeted(
        self, month: str, category_id: str, budgeted_milliunits: int
    ) -> None:
        path = (
            f"/budgets/{self._budget}/months/{month}/categories/{category_id}"
        )
        response = self._client.patch(
            path, json={"category": {"budgeted": budgeted_milliunits}}
        )
        response.raise_for_status()
