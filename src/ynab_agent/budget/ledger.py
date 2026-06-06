"""The overspend-alert dedup ledger — pure state and folds (SPEC §7).

W6 alerts a category *at most once per budget period* unless it materially
worsens (the ``should_alert`` rule in :mod:`ynab_agent.budget.overspend`). That
rule compares the current assessment against the *last alert raised this
period*, so the prior alert must survive between daily passes. This module is
that memory, kept tiny: one entry per category — the verdict and projected
month-end it last alerted at, stamped with the period it belongs to.

* ``prior`` answers "what did we last alert this category, this period?" — and
  returns ``None`` across a period boundary, so the first alert of a new month
  always fires.
* ``record`` folds a freshly-sent alert in, replacing the category's old entry
  so the tail stays one-per-category (bounded without pruning).

Pure and frozen like the rest of the domain; the durable
:class:`~ynab_agent.workflow.overspend_ledger_workflow.OverspendLedgerWorkflow`
wraps it as Temporal state, mirroring how ``alert.ledger`` sits under the
failure-alert ledger and ``learn.registry`` under the rule registry.
"""

from __future__ import annotations

from ynab_agent.budget.overspend import PriorAlert
from ynab_agent.domain.base import Frozen


class LedgerEntry(Frozen):
    """The last alert raised for one category, and the period it belongs to.

    ``period`` is the budget month as ``"YYYY-MM"`` (the caller derives it from
    the household clock), so a last-month entry reads as absent this month.
    """

    category: str
    period: str
    alert: PriorAlert


class OverspendLedgerState(Frozen):
    """The whole dedup memory — one entry per category, carried as state.

    Held as Temporal workflow state and carried across continue-as-new, so it is
    a frozen value like the rest of the domain.
    """

    entries: tuple[LedgerEntry, ...] = ()


def prior(
    state: OverspendLedgerState, category: str, period: str
) -> PriorAlert | None:
    """The category's last alert *this period*, or ``None``. Pure.

    A stored entry from a different period reads as absent, so a new month
    resets the dedupe and the first flag of the period always alerts (SPEC §7).
    """
    for entry in state.entries:
        if entry.category == category and entry.period == period:
            return entry.alert
    return None


def record(
    state: OverspendLedgerState,
    category: str,
    period: str,
    alert: PriorAlert,
) -> OverspendLedgerState:
    """Record this period's alert for ``category``, replacing any prior. Pure.

    Dropping the category's previous entry (regardless of period) keeps the tail
    at one entry per category, so the carried state never grows with continued
    alerting.
    """
    kept = tuple(entry for entry in state.entries if entry.category != category)
    return OverspendLedgerState(
        entries=(
            *kept,
            LedgerEntry(category=category, period=period, alert=alert),
        )
    )
