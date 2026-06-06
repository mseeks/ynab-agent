"""Params, identifiers, and boundary values for the balance workflow (§8).

Kept apart from :mod:`ynab_agent.workflow.balance_workflow` so the activities,
the dispatcher, and the W6 monitor can import the shapes without dragging the
workflow definition (and its sandbox import rules) along — the same split
:mod:`ynab_agent.workflow.offer_types` has from the offer workflow.
"""

from __future__ import annotations

from datetime import timedelta

from ynab_agent.budget.overspend import OverspendAssessment
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money

# The per-(category, period) balance-offer workflow id. At most one is live
# offer per category per budget month, so it is started ``REJECT_DUPLICATE`` —
# the one-offer-per-period guard mirrors the W6 monitor's own dedupe.
_BALANCE_ID_PREFIX = "balance-offer-"

# The keyword search attribute the balance workflow stamps with the overspend
# thread id, so W3 routes a reply on it back here (a coverage decision),
# never to a transaction's W2. Registered on the namespace by
# manage/search-attributes.yaml, like ``TxnThreadId`` and ``OfferThreadId``.
BALANCE_THREAD_ID = "BalanceThreadId"

# How long the offer waits for an answer before giving up. Shorter than the
# autonomy offer: a coverage decision is time-sensitive (the month is running),
# but still generous enough not to nag.
BALANCE_PATIENCE = timedelta(days=7)


def balance_workflow_id(category: str, period: str) -> str:
    """The deterministic balance-offer id for a category in a period."""
    return f"{_BALANCE_ID_PREFIX}{category}-{period}"


class BalanceParams(Frozen):
    """The balance workflow's input: the overspend, its thread, and period."""

    assessment: OverspendAssessment
    thread_id: str
    period: str


class BudgetState(Frozen):
    """A snapshot of the budget the workflow computes absolute targets from.

    ``available`` is each source's pull-able funds (category balance, plus the
    Ready-to-Assign sentinel); ``budgeted`` is each category's current budgeted
    amount. The workflow derives the absolute write targets from ``budgeted`` so
    a write retry re-sets the same value (idempotent), never double-applies.
    """

    available: dict[CategoryId, Money]
    budgeted: dict[CategoryId, Money]


class BalanceResult(Frozen):
    """The workflow's terminal outcome, for tests and the ops dashboard."""

    outcome: str
    detail: str = ""
