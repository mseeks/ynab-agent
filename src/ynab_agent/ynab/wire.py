"""The slice of the YNAB JSON wire format this agent consumes (SPEC §1, §7).

Loose, ``extra='ignore'`` models — YNAB sends far more fields than we read, and
a new field must never break a parse. These are the boundary types; the client
maps them onto the strict domain (``YnabSnapshot`` etc.), so nothing downstream
sees a wire shape.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Wire(BaseModel):
    """Base for wire models: frozen, and tolerant of unknown fields."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class WireSubtransaction(_Wire):
    """One subtransaction of a YNAB split (the fields we verify against, §3).

    A split parent carries ``category_id = null`` and one of these per line, so
    the read-back reconstructs the split end-state from them — letting a split
    write be confirmed field-by-field rather than always could-not-confirm
    (SPEC §3 r4).
    """

    amount: int
    category_id: str | None = None
    memo: str | None = None
    deleted: bool = False


class WireTransaction(_Wire):
    """A YNAB transaction as the API returns it (the fields we use)."""

    id: str
    account_id: str
    date: str
    amount: int
    approved: bool
    deleted: bool = False
    memo: str | None = None
    cleared: str = "uncleared"
    flag_color: str | None = None
    payee_id: str | None = None
    payee_name: str | None = None
    category_id: str | None = None
    import_id: str | None = None
    matched_transaction_id: str | None = None
    subtransactions: tuple[WireSubtransaction, ...] = ()


class WireCategory(_Wire):
    """A YNAB category as the API returns it (the budget figures we use)."""

    id: str
    name: str
    budgeted: int
    activity: int
    balance: int
    deleted: bool = False
    hidden: bool = False


class WireMonth(_Wire):
    """A YNAB budget month (the ``to_be_budgeted`` we read for W7, SPEC §8).

    ``to_be_budgeted`` is the month's Ready-to-Assign in milliunits — the first
    source the balancer pulls from.
    """

    month: str
    to_be_budgeted: int
