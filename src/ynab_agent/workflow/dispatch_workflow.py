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
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from ynab_agent.dispatch.classify import (
        Ignore,
        InboundKind,
        InboundMessage,
        Quarantine,
        RouteToBalance,
        RouteToInterpret,
        RouteToOffer,
        RouteToTransaction,
        classify,
    )
    from ynab_agent.domain.ids import YnabTransactionId
    from ynab_agent.workflow import alert_activities, dispatch_activities
    from ynab_agent.workflow.alerting import build_failure_alert
    from ynab_agent.workflow.constants import (
        ACTIVITY_RETRY,
        ACTIVITY_TIMEOUT,
        ALERT_BUDGET,
        ALERT_RETRY,
        ALERT_TIMEOUT,
    )
    from ynab_agent.workflow.dispatch_types import (
        DispatchParams,
        DispatchResult,
    )


@workflow.defn
class DispatchWorkflow:
    """One inbound message, classified and routed."""

    @workflow.run
    async def run(self, params: DispatchParams) -> DispatchResult:
        """Classify + route, paging once on a terminal failure (SPEC §5, §13).

        Wraps the dispatch in the same terminal-failure hook W2 uses: a
        non-retryable bug or an elapsed retry budget pages the owner once
        (deduped on the message id) before the workflow fails — so a dropped
        inbound reply or command is never silent.
        """
        try:
            return await self._run(params)
        except ActivityError as exc:
            message = params.message
            await workflow.execute_activity(
                alert_activities.alert_failure,
                build_failure_alert(
                    key=str(message.message_id),
                    context=(
                        f"inbound from {message.from_address}: "
                        f"{message.subject}"
                    ),
                    exc=exc,
                ),
                start_to_close_timeout=ALERT_TIMEOUT,
                schedule_to_close_timeout=ALERT_BUDGET,
                retry_policy=ALERT_RETRY,
            )
            raise

    async def _run(self, params: DispatchParams) -> DispatchResult:
        """Classify the message and route it (SPEC §5)."""
        message = params.message
        thread = (
            str(message.thread_id) if message.thread_id is not None else None
        )
        txn_id_str = await workflow.execute_activity(
            dispatch_activities.resolve_thread,
            thread,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        txn_id = (
            YnabTransactionId(txn_id_str) if txn_id_str is not None else None
        )
        # Only when the thread is not a transaction's do we ask whether it is a
        # live autonomy offer (a reply there is a bless-acceptance, §14.7 3b),
        # then a live balance offer (a coverage decision, §8). A thread belongs
        # to at most one, so each lookup runs only if the prior missed.
        offer_id: str | None = None
        if txn_id is None:
            offer_id = await workflow.execute_activity(
                dispatch_activities.resolve_offer_thread,
                thread,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
        balance_id: str | None = None
        if txn_id is None and offer_id is None:
            balance_id = await workflow.execute_activity(
                dispatch_activities.resolve_balance_thread,
                thread,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
        decision = classify(
            message,
            params.allowlist,
            txn_id=txn_id,
            offer_id=offer_id,
            balance_id=balance_id,
        )

        match decision:
            case RouteToTransaction(txn_id=tid):
                await workflow.execute_activity(
                    dispatch_activities.signal_transaction,
                    args=[tid, message],
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=ACTIVITY_RETRY,
                )
                return DispatchResult(action="transaction")
            case RouteToOffer(offer_id=oid):
                await workflow.execute_activity(
                    dispatch_activities.signal_offer,
                    args=[oid, message],
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=ACTIVITY_RETRY,
                )
                return DispatchResult(action="offer")
            case RouteToBalance(balance_id=bid):
                await workflow.execute_activity(
                    dispatch_activities.signal_balance,
                    args=[bid, message],
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=ACTIVITY_RETRY,
                )
                return DispatchResult(action="balance")
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
            retry_policy=ACTIVITY_RETRY,
        )
        if kind is InboundKind.RECEIPT:
            await workflow.execute_activity(
                dispatch_activities.route_receipt,
                message,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
            return DispatchResult(action="receipt")
        if kind is InboundKind.COMMAND:
            await workflow.execute_activity(
                dispatch_activities.handle_command,
                message,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
            return DispatchResult(action="command")
        return DispatchResult(action="ignore", detail="classified as noise")
