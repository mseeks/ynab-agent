"""The I/O ports of the W3 inbound dispatcher, as Temporal activities.

Kept separate from the other workflows' activity modules so each workflow's
sandbox import graph stays minimal (see ``poll_activities``). Heavy clients
(Temporal, the model stack) are imported lazily inside the bodies so they never
enter the workflow sandbox.
"""

from __future__ import annotations

import contextlib

from temporalio import activity

from ynab_agent.dispatch.classify import InboundKind, InboundMessage
from ynab_agent.workflow.temporal_client import client, task_queue


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
async def route_receipt(_message: InboundMessage) -> None:
    """Hand a forwarded receipt to the W4 join (SPEC §5b).

    No-op in v1: the receipt join (W4) is out of the core-triage scope, and the
    agent does not advertise receipt forwarding, so receipts do not arrive yet.
    Wired when W4 lands (the message arg is kept for that contract).
    """
    return None


@activity.defn
async def handle_command(message: InboundMessage) -> None:
    """Parse a standing command and bless the rule it grants (SPEC §5c, §14).

    The owner's direct opt-in: "always categorize X as Y" becomes an
    ``ExplicitCommand`` signalled to the durable registry's ``bless``, which
    trusts the rule for auto-apply (``source=human_explicit``). Anything not a
    clear bless — a question or comment — is a deliberate no-op (the parser
    declines it), so a stray message never grants autonomy.
    """
    import asyncio

    from temporalio.common import WorkflowIDConflictPolicy

    from ynab_agent.agentic.command import (
        CommandRequest,
        parse_command,
        to_explicit_command,
    )
    from ynab_agent.agentic.enrich import CandidateCategory
    from ynab_agent.workflow.registry_types import (
        REGISTRY_WORKFLOW_ID,
        RegistryParams,
    )
    from ynab_agent.workflow.temporal_client import client, task_queue
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
    command = to_explicit_command(reading)
    if command is None:
        return
    temporal = await client()
    await temporal.start_workflow(
        "RuleRegistryWorkflow",
        RegistryParams(),
        id=REGISTRY_WORKFLOW_ID,
        task_queue=task_queue(),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="bless",
        start_signal_args=[command],
    )
