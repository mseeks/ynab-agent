"""Params and request shapes for the failure-alert path (SPEC §13).

Kept apart from the workflow/activity modules so both — and the W2 hook that
constructs a :class:`FailureAlert` — can import the shapes without dragging the
workflow definition (and its sandbox import rules) along. Mirrors
``registry_types`` sitting beside the rule registry.
"""

from __future__ import annotations

import datetime

from ynab_agent.alert.ledger import LedgerState
from ynab_agent.domain.base import Frozen

# The singleton dedup-ledger workflow id: one alert ledger per deployment, born
# on the first alert and living forever via continue-as-new.
ALERT_LEDGER_WORKFLOW_ID = "ynab-alert-ledger"


class LedgerParams(Frozen):
    """The ledger's run input — the carried state across continue-as-new."""

    state: LedgerState = LedgerState()


class ShouldNotifyRequest(Frozen):
    """A dedup check: would an alert for ``key`` fire at ``now``?

    ``now`` is supplied by the caller (the alert activity reads real time) so
    the ledger's ``should_notify`` query stays a pure function of state + input.
    """

    key: str
    now: datetime.datetime


class FailureAlert(Frozen):
    """One operator alert: a dedup key and the already-composed push text.

    The W2 hook composes ``title``/``body`` from the terminal ``ActivityError``
    (deterministically, from replay-safe fields); the activity adds priority and
    tags and pushes it. ``key`` drives dedup — the transaction id, so the hourly
    re-fire of a still-broken transaction collapses to one alert per cooldown.
    """

    key: str
    title: str
    body: str
