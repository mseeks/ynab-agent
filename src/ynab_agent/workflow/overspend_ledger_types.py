"""Params and request shapes for the overspend-alert dedup ledger (W6, SPEC §7).

Kept apart from :mod:`ynab_agent.workflow.overspend_ledger_workflow` so the
monitor activities can import the request/param shapes without dragging the
workflow definition (and its sandbox import rules) along. Mirrors
``alert_types`` beside the failure-alert ledger and ``registry_types`` beside
the rule registry.
"""

from __future__ import annotations

from ynab_agent.budget.ledger import OverspendLedgerState
from ynab_agent.budget.overspend import PriorAlert
from ynab_agent.domain.base import Frozen

# The singleton ledger workflow id: one overspend-alert dedup table per
# deployment, born on the first alert and living forever via continue-as-new.
OVERSPEND_LEDGER_WORKFLOW_ID = "ynab-overspend-ledger"


class LedgerParams(Frozen):
    """The ledger's run input — the carried state across continue-as-new."""

    state: OverspendLedgerState = OverspendLedgerState()


class PriorRequest(Frozen):
    """A dedup read: the category's last alert in ``period``, if any.

    ``period`` is supplied by the caller (the activity reads real time) so the
    ledger's ``prior`` query stays a pure function of state + input — the same
    shape as ``alert_types.ShouldNotifyRequest``.
    """

    category: str
    period: str


class RecordRequest(Frozen):
    """Record a freshly-sent alert for ``category`` in ``period``."""

    category: str
    period: str
    alert: PriorAlert
