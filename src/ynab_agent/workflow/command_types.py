"""Params and id for the command-confirm workflow (SPEC §5c, §0.6, §14.2).

Kept apart from :mod:`ynab_agent.workflow.command_workflow` so the activities
and the dispatcher can import the shapes without dragging the workflow
definition (and its sandbox rules) along — the same split
:mod:`ynab_agent.workflow.offer_types` has from the offer workflow. The reply
routing reuses the offer's ``OfferThreadId`` search attribute: a reply on a
command-confirm thread is an autonomy decision, exactly like an offer reply, so
W3 routes it back the same way (no new dispatch path).
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from ynab_agent.domain.base import Frozen
from ynab_agent.learn.events import ExplicitCommand

_CONFIRM_ID_PREFIX = "command-confirm-"

# How long the read-back waits for the owner's one-word confirm. Shorter than
# the proactive offer's window: this is a reply to the owner's *own* just-sent
# command, so a prompt confirm is expected; a much-later command re-opens a
# fresh confirm (ALLOW_DUPLICATE), and silence simply never blesses.
COMMAND_CONFIRM_PATIENCE = timedelta(days=3)


def command_confirm_id(command: ExplicitCommand) -> str:
    """A deterministic confirm-workflow id, keyed by the (payee, category).

    Same command → same id, so a resend while a confirm is already pending is a
    no-op (the start conflicts and is swallowed) — "idempotent against resends"
    (SPEC §5c) — while a later command after the confirm closed re-opens a fresh
    one (the start uses ``ALLOW_DUPLICATE``).
    """
    allocation = command.action.allocation
    category = getattr(allocation, "category", "split")
    key = f"{command.match.payee_pattern}|{category}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return f"{_CONFIRM_ID_PREFIX}{digest}"


class CommandConfirmParams(Frozen):
    """The confirm workflow's run input: the command awaiting a one-word yes."""

    command: ExplicitCommand
