"""The I/O ports of the W3 inbound dispatcher, as Temporal activities.

Kept separate from the other workflows' activity modules so each workflow's
sandbox import graph stays minimal (see ``poll_activities``). Heavy clients
(Temporal, the model stack) are imported lazily inside the bodies so they never
enter the workflow sandbox.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from temporalio import activity

from ynab_agent.dispatch.classify import InboundKind, InboundMessage
from ynab_agent.workflow.temporal_client import client, task_queue

if TYPE_CHECKING:
    from ynab_agent.domain.rule import Rule
    from ynab_agent.workflow.registry_types import RegistryView


@activity.defn
async def resolve_thread(thread_id: str | None) -> str | None:
    """Resolve an AgentMail thread id to its txn id, or None (SPEC §5).

    The per-transaction workflow stamps its AgentMail thread id into the
    ``TxnThreadId`` search attribute, so a reply's thread maps back to that
    workflow through a Temporal visibility query. The workflow id *is* the YNAB
    transaction id (started ``REJECT_DUPLICATE`` on it), so the matching
    execution's id is the answer — there is no stored thread↔txn table (SPEC
    §0.5, store-free). ``None`` when the thread belongs to no live transaction.
    """
    if thread_id is None:
        return None
    temporal = await client()
    # The thread id is an AgentMail token, but quote-escape defensively so the
    # visibility query stays well-formed.
    safe = thread_id.replace('"', '\\"')
    async for execution in temporal.list_workflows(
        query=f'TxnThreadId = "{safe}"'
    ):
        return execution.id
    return None


@activity.defn
async def resolve_offer_thread(thread_id: str | None) -> str | None:
    """Resolve an AgentMail thread to a *running* offer workflow id (3b).

    The autonomy-offer workflow stamps its thread into the ``OfferThreadId``
    search attribute, so a reply on that thread maps back to it the same way
    ``resolve_thread`` maps a transaction. Filtered to ``Running`` executions so
    a reply landing after the offer has closed is *not* routed (it falls through
    to the command path, the documented late-accept fallback) rather than
    resurrecting a finished offer. ``None`` when no live offer owns the thread.
    """
    if thread_id is None:
        return None
    from ynab_agent.workflow.offer_types import OFFER_THREAD_ID

    temporal = await client()
    safe = thread_id.replace('"', '\\"')
    query = f'{OFFER_THREAD_ID} = "{safe}" AND ExecutionStatus = "Running"'
    async for execution in temporal.list_workflows(query=query):
        return execution.id
    return None


@activity.defn
async def signal_offer(offer_id: str, message: InboundMessage) -> None:
    """Deliver a reply to its autonomy-offer workflow (SPEC §14.7 3b).

    A plain signal (not signal-with-start): the offer must be live to receive it
    — ``resolve_offer_thread`` already filtered to running executions — so a
    closed offer is never resurrected. A missing handle is a benign no-op.
    """
    from temporalio.service import RPCError

    temporal = await client()
    handle = temporal.get_workflow_handle(offer_id)
    with contextlib.suppress(RPCError):
        await handle.signal("submit_response", message)


@activity.defn
async def resolve_balance_thread(thread_id: str | None) -> str | None:
    """Resolve an AgentMail thread to a *running* balance workflow id (§8).

    The balance workflow stamps the overspend thread into ``BalanceThreadId``,
    so a reply there maps back to it the same way ``resolve_offer_thread`` maps
    an offer. Filtered to ``Running`` so a reply after the offer has closed is
    not routed (it falls through to the command path). ``None`` when no live
    balance offer owns the thread.
    """
    if thread_id is None:
        return None
    from ynab_agent.workflow.balance_types import BALANCE_THREAD_ID

    temporal = await client()
    safe = thread_id.replace('"', '\\"')
    query = f'{BALANCE_THREAD_ID} = "{safe}" AND ExecutionStatus = "Running"'
    async for execution in temporal.list_workflows(query=query):
        return execution.id
    return None


@activity.defn
async def signal_balance(balance_id: str, message: InboundMessage) -> None:
    """Deliver a reply to its balance workflow (SPEC §8).

    A plain signal: the workflow must be live to receive it —
    ``resolve_balance_thread`` already filtered to running executions. A missing
    handle is a benign no-op.
    """
    from temporalio.service import RPCError

    temporal = await client()
    handle = temporal.get_workflow_handle(balance_id)
    with contextlib.suppress(RPCError):
        await handle.signal("submit_response", message)


@activity.defn
async def classify_inbound(message: InboundMessage) -> InboundKind:
    """Agentically classify a non-thread message: receipt, command, or noise.

    The model stack is imported lazily here so it never enters the workflow
    sandbox. The model only labels the message; the deterministic dispatcher
    routes it (SPEC §5).
    """
    from ynab_agent.agentic.classify import classify_inbound as run_classifier
    from ynab_agent.agentic.classify import to_kind

    return to_kind(await run_classifier(message))


@activity.defn
async def signal_transaction(txn_id: str, message: InboundMessage) -> None:
    """Deliver a reply to the transaction's W2 (SPEC §5a).

    Signal-with-start on ``submit_inbound``: a running W2 (the common case — it
    asked the question) receives the reply and wakes; if the transaction's run
    has closed, a fresh W2 starts with the reply buffered and re-triages. The
    workflow id is the YNAB transaction id, so no thread↔txn table is needed.
    """
    from ynab_agent.domain.ids import YnabTransactionId
    from ynab_agent.domain.signals import ReplySignal
    from ynab_agent.workflow.types import TransactionParams

    if message.thread_id is None:
        # RouteToTransaction only fires for a thread-matched message, so a
        # missing thread id here is a routing bug, not an expected input.
        msg = f"signal for {txn_id} has no thread id"
        raise RuntimeError(msg)
    reply = ReplySignal(
        thread_id=message.thread_id,
        message_id=message.message_id,
        from_address=message.from_address,
        text=message.body,
    )
    temporal = await client()
    await temporal.start_workflow(
        "TransactionWorkflow",
        TransactionParams(ynab_id=YnabTransactionId(txn_id)),
        id=txn_id,
        task_queue=task_queue(),
        start_signal="submit_inbound",
        start_signal_args=[reply],
    )


@activity.defn
async def route_receipt(message: InboundMessage) -> None:
    """Read a forwarded receipt, park it, and start its join (SPEC §5b, §6).

    The W4 entry point: the extraction agent reads the forward into a
    :class:`~ynab_agent.domain.receipt.Receipt` (deterministically converted —
    exact money, real dates). A parseable receipt is parked in the durable
    ledger (idempotent on the message id), acknowledged on its own thread
    naming what was read, and a first join attempt starts immediately; a
    forward that cannot be read as a receipt gets an honest "couldn't read
    this" note instead of parking junk. Every send dedups on the message id,
    so a webhook retry never double-replies; a message with no thread to
    reply on is a no-op.
    """
    import asyncio
    import datetime

    from ynab_agent.agentic.compose import (
        render_receipt_ack,
        render_receipt_ack_html,
        render_receipt_unparseable,
    )
    from ynab_agent.agentic.receipt_parse import (
        ReceiptParseRequest,
        parse_receipt,
        to_receipt,
    )
    from ynab_agent.domain.ids import ReceiptId
    from ynab_agent.domain.receipt import receipt_summary
    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings
    from ynab_agent.workflow import receipt_activities

    if message.thread_id is None:
        return
    parsed = await parse_receipt(
        ReceiptParseRequest(subject=message.subject, body=message.body)
    )
    receipt = to_receipt(
        parsed,
        receipt_id=ReceiptId(str(message.message_id)),
        now=datetime.datetime.now(datetime.UTC),
        message_id=message.message_id,
        thread_id=message.thread_id,
    )
    settings = Settings()
    mail = MailClient.from_env()
    if receipt is None:
        # A distinct label from the ack: the model parse re-runs on an
        # activity retry, and two semantically opposite messages must never
        # share one dedup key (a flipped verdict would suppress the truth).
        await asyncio.to_thread(
            mail.send_on_thread,
            inbox_id=settings.inbox,
            thread_id=str(message.thread_id),
            body=render_receipt_unparseable(),
            seq_label=f"yarcpt-unread-{message.message_id}",
            to=list(settings.owners),
        )
        return
    await receipt_activities.park_in_ledger(receipt)
    await asyncio.to_thread(
        mail.send_on_thread,
        inbox_id=settings.inbox,
        thread_id=str(message.thread_id),
        body=render_receipt_ack(receipt_summary(receipt)),
        seq_label=f"yarcpt-ack-{message.message_id}",
        to=list(settings.owners),
        html=render_receipt_ack_html(receipt),
    )
    await receipt_activities.start_join(receipt)


async def _reply(message: InboundMessage, body: str, tag: str) -> None:
    """Reply on the message's own thread, deduped on the inbound message id."""
    import asyncio

    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings

    if message.thread_id is None:
        return
    settings = Settings()
    mail = MailClient.from_env()
    await asyncio.to_thread(
        mail.send_on_thread,
        inbox_id=settings.inbox,
        thread_id=str(message.thread_id),
        body=body,
        seq_label=f"yacmd-{tag}-{message.message_id}",
        to=list(settings.owners),
    )


async def _registry_view() -> RegistryView | None:
    """The registry's current view, or ``None`` before its first signal."""
    from temporalio.service import RPCError

    from ynab_agent.workflow.registry_types import (
        REGISTRY_WORKFLOW_ID,
        RegistryView,
    )

    temporal = await client()
    handle = temporal.get_workflow_handle(REGISTRY_WORKFLOW_ID)
    try:
        view: RegistryView = await handle.query(
            "view", result_type=RegistryView
        )
    except RPCError:
        return None
    return view


def _category_display(rule: Rule, names: dict[str, str]) -> str:
    """A rule's target category by name (best effort; splits stay 'a split')."""
    from ynab_agent.domain.allocations import ProposedCategory

    allocation = rule.action.allocation
    if isinstance(allocation, ProposedCategory):
        cid = str(allocation.category)
        return names.get(cid, cid)
    return "a split"


async def _revoke_and_reply(message: InboundMessage, payee: str) -> None:
    """Strip a payee's standing autonomy and confirm by reply (SPEC §14.5).

    Revoking is the safe direction, so unlike a bless it takes effect with no
    read-back. The registry is consulted first so the reply is honest: a
    payee with no blessed rule gets "nothing changed", not a fake "done".
    """
    from temporalio.common import WorkflowIDConflictPolicy

    from ynab_agent.agentic.compose import (
        render_revoke_nothing,
        render_revoked,
    )
    from ynab_agent.domain.enums import RuleSource
    from ynab_agent.workflow.registry_types import (
        REGISTRY_WORKFLOW_ID,
        RegistryParams,
    )

    view = await _registry_view()
    rules = view.rules if view is not None else ()
    lowered = payee.lower()
    blessed = [
        rule
        for rule in rules
        if rule.source is RuleSource.HUMAN_EXPLICIT
        and rule.match.payee_pattern.lower() in lowered
    ]
    if not blessed:
        await _reply(message, render_revoke_nothing(payee), "revoke")
        return
    temporal = await client()
    await temporal.start_workflow(
        "RuleRegistryWorkflow",
        RegistryParams(),
        id=REGISTRY_WORKFLOW_ID,
        task_queue=task_queue(),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="revoke",
        start_signal_args=[payee],
    )
    await _reply(message, render_revoked(payee), "revoke")


async def _reply_rules_list(
    message: InboundMessage, names: dict[str, str]
) -> None:
    """Answer "list my rules" with the autonomy ladder in plain words (§14)."""
    from ynab_agent.agentic.compose import render_rules_list
    from ynab_agent.domain.enums import RuleSource, TrustState

    view = await _registry_view()
    rules = view.rules if view is not None else ()
    blessed = tuple(
        (rule.match.payee_pattern, _category_display(rule, names))
        for rule in rules
        if rule.source is RuleSource.HUMAN_EXPLICIT
    )
    eligible = tuple(
        (rule.match.payee_pattern, _category_display(rule, names))
        for rule in rules
        if rule.source is RuleSource.LEARNED
        and rule.trust is TrustState.TRUSTED
    )
    observing = sum(
        1
        for rule in rules
        if rule.source is RuleSource.LEARNED
        and rule.trust is not TrustState.TRUSTED
    )
    await _reply(
        message, render_rules_list(blessed, eligible, observing), "rules"
    )


@activity.defn
async def handle_command(message: InboundMessage) -> None:
    """Act on a standing-instruction message (SPEC §5c, §14.5).

    The verbs of the autonomy journey, parsed by the model and acted on
    deterministically:

      - **bless** ("always categorize X as Y") grants autonomy, so it is
        *not* blessed inline (SPEC §0.6) — a one-shot
        ``CommandConfirmWorkflow`` opens a read-back and blesses only on a
        one-word confirm. Keyed by (payee, category): a resend while one is
        pending is a no-op.
      - **revoke** ("stop auto-handling X") removes autonomy — the safe
        direction, so it takes effect immediately, with an honest reply
        either way.
      - **list_rules** / **help** answer with the rule ladder / the
        capability sheet on the message's own thread.

    Anything else is a deliberate no-op (the parser declines it), so a stray
    message never grants or changes anything.
    """
    import asyncio

    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from ynab_agent.agentic.command import (
        CommandKind,
        CommandRequest,
        parse_command,
        to_explicit_command,
    )
    from ynab_agent.agentic.compose import render_help
    from ynab_agent.agentic.enrich import CandidateCategory
    from ynab_agent.workflow.command_types import (
        CommandConfirmParams,
        command_confirm_id,
    )
    from ynab_agent.ynab.client import YnabClient

    ynab = YnabClient.from_env()
    spends = await asyncio.to_thread(ynab.category_spends)
    candidates = tuple(
        CandidateCategory(id=str(spend.category), name=spend.name)
        for spend in spends
    )
    if not candidates:
        return
    reading = await parse_command(
        CommandRequest(command_text=message.body, candidates=candidates)
    )
    if reading.kind is CommandKind.REVOKE and reading.payee_pattern:
        await _revoke_and_reply(message, reading.payee_pattern)
        return
    if reading.kind is CommandKind.LIST_RULES:
        await _reply_rules_list(message, {c.id: c.name for c in candidates})
        return
    if reading.kind is CommandKind.HELP:
        await _reply(message, render_help(), "help")
        return
    command = to_explicit_command(reading, candidates)
    if command is None:
        return
    temporal = await client()
    try:
        await temporal.start_workflow(
            "CommandConfirmWorkflow",
            CommandConfirmParams(command=command),
            id=command_confirm_id(command),
            task_queue=task_queue(),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        # A confirm for this (payee, category) is already pending: a resend is a
        # no-op (SPEC §5c, idempotent against resends).
        return
