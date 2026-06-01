"""The I/O ports of the W3 inbound dispatcher, as Temporal activities.

Kept separate from the other workflows' activity modules so each workflow's
sandbox import graph stays minimal (see ``poll_activities``). Heavy clients
(Temporal, the model stack) are imported lazily inside the bodies so they never
enter the workflow sandbox.
"""

from __future__ import annotations

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
async def handle_command(_message: InboundMessage) -> None:
    """Run an ad-hoc command through the command handler (SPEC §5c).

    No-op in v1: ad-hoc commands are out of the core-triage scope. Wired when
    the command handler lands (the message arg is kept for that contract).
    """
    return None
