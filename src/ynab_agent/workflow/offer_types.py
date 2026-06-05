"""Params and identifiers for the autonomy-offer workflow (SPEC §14.7 3b).

Kept apart from :mod:`ynab_agent.workflow.offer_workflow` so the activities and
the dispatcher can import the param/id shapes without dragging the workflow
definition (and its sandbox import rules) along — the same split
:mod:`ynab_agent.workflow.registry_types` has from the registry workflow.
"""

from __future__ import annotations

from datetime import timedelta

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.rule import Rule

# The per-rule offer workflow id. There is at most one live offer per rule, so
# the id is derived from the rule id and started ``REJECT_DUPLICATE`` — the
# authoritative one-time guard for the proactive prompt.
_OFFER_ID_PREFIX = "autonomy-offer-"

# The keyword search attribute the offer workflow stamps with its thread id, so
# W3 routes a reply on that thread back to *this* workflow (a bless-acceptance),
# never to a transaction's W2 (a category reply). Registered on the namespace by
# manage/search-attributes.yaml, like ``TxnThreadId``.
OFFER_THREAD_ID = "OfferThreadId"

# How long the offer waits for an answer before giving up. Generous: the prompt
# is one-time and low-urgency, and a much-later "yes" can still bless via the
# explicit command path (3a).
OFFER_PATIENCE = timedelta(days=14)


def offer_workflow_id(rule_id: str) -> str:
    """The deterministic offer workflow id for a rule."""
    return f"{_OFFER_ID_PREFIX}{rule_id}"


class OfferParams(Frozen):
    """The offer workflow's run input: the eligible rule being offered."""

    rule: Rule
