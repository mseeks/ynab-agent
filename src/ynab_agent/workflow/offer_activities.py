"""The I/O ports of the autonomy-offer subsystem, as Temporal activities.

Kept separate from the other workflows' activity modules so each workflow's
sandbox import graph stays minimal (see ``poll_activities``). Heavy clients
(Temporal, YNAB, AgentMail) are imported lazily inside the bodies so they never
enter a workflow sandbox.

These wire SPEC §14.7 increment 3b: the registry starts an offer
(:func:`start_autonomy_offer`); the offer workflow opens its thread
(:func:`open_offer_thread`), reads the reply (:func:`interpret_offer_reply`),
and on a yes blesses the rule and confirms (:func:`accept_offer`) or, on a no,
sends a brief note (:func:`decline_offer`).
"""

from __future__ import annotations

from temporalio import activity

from ynab_agent.domain.enums import OfferVerdict
from ynab_agent.domain.rule import Rule


def _category_name(rule: Rule, names: dict[str, str]) -> str:
    """The display name of the category a rule auto-applies (best effort)."""
    from ynab_agent.domain.allocations import ProposedCategory

    allocation = rule.action.allocation
    if isinstance(allocation, ProposedCategory):
        cid = str(allocation.category)
        return names.get(cid, cid)
    return "a split"


async def _category_names() -> dict[str, str]:
    """A category-id→name map read from YNAB (the source of truth)."""
    import asyncio

    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    spends = await asyncio.to_thread(client.category_spends)
    return {str(spend.category): spend.name for spend in spends}


@activity.defn
async def start_autonomy_offer(rule: Rule) -> None:
    """Start the one-time offer workflow for an eligible rule (SPEC §14.7 3b).

    Started ``REJECT_DUPLICATE`` on the per-rule id, so a re-trigger (a later
    confirmation, an activity retry) is a no-op rather than a second prompt —
    the authoritative one-time guard. An already-started offer is success.
    """
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from ynab_agent.workflow.offer_types import (
        OfferParams,
        offer_workflow_id,
    )
    from ynab_agent.workflow.temporal_client import client, task_queue

    temporal = await client()
    try:
        await temporal.start_workflow(
            "AutonomyOfferWorkflow",
            OfferParams(rule=rule),
            id=offer_workflow_id(str(rule.id)),
            task_queue=task_queue(),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        return


@activity.defn
async def open_offer_thread(rule: Rule) -> str:
    """Open the offer's email thread and return its id (SPEC §14.7 3b).

    A new standalone thread (its own ``yaoffer-`` idempotency label, so a retry
    re-finds it rather than sending twice), addressed to the owners. The payee
    and the category name come from the rule and a YNAB read at send time.
    """
    import asyncio

    from ynab_agent.agentic.compose import render_autonomy_offer
    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings

    settings = Settings()
    names = await _category_names()
    payee = rule.match.payee_pattern
    category = _category_name(rule, names)
    body = render_autonomy_offer(payee, category)
    mail = MailClient.from_env()
    return await asyncio.to_thread(
        mail.open_thread,
        inbox_id=settings.inbox,
        to=list(settings.owners),
        subject=f"Auto-handle {payee}?",
        body=body,
        txn_label=f"yaoffer-{rule.id}",
    )


@activity.defn
async def interpret_offer_reply(reply_text: str, payee: str) -> OfferVerdict:
    """Read a free-form reply to the offer into a verdict (SPEC §14.7 3b).

    The model stack is imported lazily here so it never enters a workflow
    sandbox. Defaults to ``UNCLEAR`` on doubt — never ``ACCEPT`` — so standing
    autonomy is never granted on an ambiguous reply.
    """
    from ynab_agent.agentic.offer import (
        OfferReplyRequest,
        interpret_offer,
        to_verdict,
    )

    reading = await interpret_offer(
        OfferReplyRequest(reply_text=reply_text, payee=payee)
    )
    return to_verdict(reading)


@activity.defn
async def accept_offer(rule: Rule, thread_id: str) -> None:
    """Bless the rule and confirm — the owner accepted the offer (SPEC §14.7).

    Signals the durable registry's ``bless_existing`` (signal-with-start,
    reusing the running singleton) to flip the rule to ``human_explicit``, then
    sends a confirmation on the offer thread (idempotent on its accept label, so
    a retry never double-sends).
    """
    import asyncio

    from temporalio.common import WorkflowIDConflictPolicy

    from ynab_agent.agentic.compose import render_offer_accepted
    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings
    from ynab_agent.workflow.registry_types import (
        REGISTRY_WORKFLOW_ID,
        RegistryParams,
    )
    from ynab_agent.workflow.temporal_client import client, task_queue

    temporal = await client()
    await temporal.start_workflow(
        "RuleRegistryWorkflow",
        RegistryParams(),
        id=REGISTRY_WORKFLOW_ID,
        task_queue=task_queue(),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="bless_existing",
        start_signal_args=[str(rule.id)],
    )

    settings = Settings()
    names = await _category_names()
    body = render_offer_accepted(
        rule.match.payee_pattern, _category_name(rule, names)
    )
    mail = MailClient.from_env()
    await asyncio.to_thread(
        mail.send_on_thread,
        inbox_id=settings.inbox,
        thread_id=thread_id,
        body=body,
        seq_label=f"yaoffer-accept-{rule.id}",
    )


@activity.defn
async def decline_offer(rule: Rule, thread_id: str) -> None:
    """Send the brief "I'll keep proposing" note — owner declined (§14.7)."""
    import asyncio

    from ynab_agent.agentic.compose import render_offer_declined
    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings

    settings = Settings()
    body = render_offer_declined(rule.match.payee_pattern)
    mail = MailClient.from_env()
    await asyncio.to_thread(
        mail.send_on_thread,
        inbox_id=settings.inbox,
        thread_id=thread_id,
        body=body,
        seq_label=f"yaoffer-decline-{rule.id}",
    )
