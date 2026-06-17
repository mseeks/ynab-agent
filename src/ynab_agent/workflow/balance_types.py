"""Params, identifiers, and boundary values for the balance workflow (§8).

Kept apart from :mod:`ynab_agent.workflow.balance_workflow` so the activities,
the dispatcher, and the W6 monitor can import the shapes without dragging the
workflow definition (and its sandbox import rules) along — the same split
:mod:`ynab_agent.workflow.offer_types` has from the offer workflow.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import Field

from ynab_agent.budget.overspend import OverspendAssessment
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money

# The per-period coordinated balance-offer workflow id. At most one live
# coverage offer per budget month, so it is started ``REJECT_DUPLICATE`` — one
# coordinated plan per pass over one shared donor pool (SPEC §8, #46), which
# makes a double-drain (two needs claiming the same donor) impossible.
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


def balance_workflow_id(period: str) -> str:
    """The deterministic coordinated balance-offer id for a period (#46)."""
    return f"{_BALANCE_ID_PREFIX}{period}"


class BalanceParams(Frozen):
    """The coordinated balancer's input: the pass's overspends and period (#46).

    Every over/trending category from one monitor pass is covered by a single
    plan over one shared donor pool, so the balancer creates its own per-period
    offer thread (there is no single alert thread to inherit).
    """

    assessments: tuple[OverspendAssessment, ...]
    period: str


class BudgetState(Frozen):
    """A snapshot of the budget the workflow computes absolute targets from.

    ``available`` is each source's pull-able funds (category balance, plus the
    Ready-to-Assign sentinel); ``slack`` is each source's protected drawable
    (what it can give after its own projected spend); ``budgeted`` is each
    category's current budgeted amount. The workflow derives the absolute write
    targets from ``budgeted`` so a write retry re-sets the same value
    (idempotent), never double-applies, and the apply-time guard checks moves
    against ``slack``.
    """

    available: dict[CategoryId, Money]
    budgeted: dict[CategoryId, Money]
    slack: dict[CategoryId, Money] = Field(default_factory=dict)


class CoordinatedReplyResult(Frozen):
    """The owner's reply to the coordinated plan, read into a branch (#46).

    ``verdict`` is ``"apply"`` (apply the whole offered plan), ``"decline"``, or
    ``"clarify"`` (the reply asked for a change or was unclear; ``question`` is
    sent back). Keeping the verdict deterministic here, apart from the agent's
    own schema, is the seam the workflow branches on.
    """

    verdict: str
    question: str = ""


class BalanceResult(Frozen):
    """The workflow's terminal outcome, for tests and the ops dashboard."""

    outcome: str
    detail: str = ""
