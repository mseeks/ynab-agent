"""The I/O ports of the W3 inbound dispatcher, as Temporal activities.

Kept separate from the other workflows' activity modules so each workflow's
sandbox import graph stays minimal (see ``poll_activities``). Stubbed; the
webhook handler, thread lookup, and routing are wired later.
"""

from __future__ import annotations

from temporalio import activity

from ynab_agent.dispatch.classify import InboundKind, InboundMessage
from ynab_agent.workflow.temporal_client import client

_STUB = "workflow activity stub — register a real or mock implementation"


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
    """Agentically classify a non-thread message: receipt, command, or noise."""
    raise NotImplementedError(_STUB)


@activity.defn
async def signal_transaction(txn_id: str, message: InboundMessage) -> None:
    """Signal-with-start the transaction's W2 with the reply (SPEC §5a)."""
    raise NotImplementedError(_STUB)


@activity.defn
async def route_receipt(message: InboundMessage) -> None:
    """Hand a forwarded receipt to the W4 join (SPEC §5b)."""
    raise NotImplementedError(_STUB)


@activity.defn
async def handle_command(message: InboundMessage) -> None:
    """Run an ad-hoc command through the command handler (SPEC §5c)."""
    raise NotImplementedError(_STUB)
