"""W3 · the Inbound Dispatcher (SPEC §5).

A short workflow per inbound AgentMail message (idempotent on the message id).
It runs the pure :func:`~ynab_agent.dispatch.classify.classify` decision, then
executes it: a reply on a known thread signals that W2; an un-threaded message
is classified agentically into a receipt (→ W4) or a command; quarantine and
ignore do nothing. Fired off a signed webhook; the trigger is infrastructure.
"""

from __future__ import annotations

from typing import assert_never

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from ynab_agent.dispatch.classify import (
        Ignore,
        InboundKind,
        InboundMessage,
        Quarantine,
        RouteToInterpret,
        RouteToTransaction,
        classify,
    )
    from ynab_agent.domain.ids import YnabTransactionId
    from ynab_agent.workflow import dispatch_activities
    from ynab_agent.workflow.constants import ACTIVITY_TIMEOUT
    from ynab_agent.workflow.dispatch_types import (
        DispatchParams,
        DispatchResult,
    )


@workflow.defn
class DispatchWorkflow:
    """One inbound message, classified and routed."""

    @workflow.run
    async def run(self, params: DispatchParams) -> DispatchResult:
        """Classify the message and route it (SPEC §5)."""
        message = params.message
        thread = (
            str(message.thread_id) if message.thread_id is not None else None
        )
        txn_id_str = await workflow.execute_activity(
            dispatch_activities.resolve_thread,
            thread,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
        )
        txn_id = (
            YnabTransactionId(txn_id_str) if txn_id_str is not None else None
        )
        decision = classify(message, params.allowlist, txn_id=txn_id)

        match decision:
            case RouteToTransaction(txn_id=tid):
                await workflow.execute_activity(
                    dispatch_activities.signal_transaction,
                    args=[tid, message],
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                )
                return DispatchResult(action="transaction")
            case RouteToInterpret():
                return await self._interpret(message)
            case Quarantine(reason=reason):
                return DispatchResult(action="quarantine", detail=reason)
            case Ignore(reason=reason):
                return DispatchResult(action="ignore", detail=reason)
        assert_never(decision)

    async def _interpret(self, message: InboundMessage) -> DispatchResult:
        kind = await workflow.execute_activity(
            dispatch_activities.classify_inbound,
            message,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
        )
        if kind is InboundKind.RECEIPT:
            await workflow.execute_activity(
                dispatch_activities.route_receipt,
                message,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
            )
            return DispatchResult(action="receipt")
        if kind is InboundKind.COMMAND:
            await workflow.execute_activity(
                dispatch_activities.handle_command,
                message,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
            )
            return DispatchResult(action="command")
        return DispatchResult(action="ignore", detail="classified as noise")
