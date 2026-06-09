"""Params and request shapes for the auto-action circuit-breaker ledger.

Kept apart from the workflow/activity modules so both — and the enrich activity
that reads the counters — import the shapes without dragging the workflow
definition (and its sandbox rules) along. Mirrors ``alert_types`` /
``overspend_ledger_types`` sitting beside their ledgers.
"""

from __future__ import annotations

import datetime

from ynab_agent.domain.base import Frozen
from ynab_agent.policy.auto_action_ledger import AutoActionLedgerState

# The singleton circuit-breaker ledger workflow id: one per deployment, born on
# the first auto-action and living forever via continue-as-new.
AUTO_ACTION_LEDGER_WORKFLOW_ID = "ynab-auto-action-ledger"


class LedgerParams(Frozen):
    """The ledger's run input — the carried state across continue-as-new."""

    state: AutoActionLedgerState = AutoActionLedgerState()


class CountersRequest(Frozen):
    """A counts read at ``now``.

    ``now`` is supplied by the caller (the enrich activity reads real time) so
    the ledger's ``counters`` query stays a pure function of state + input — a
    query cannot read the wall clock.
    """

    now: datetime.datetime
