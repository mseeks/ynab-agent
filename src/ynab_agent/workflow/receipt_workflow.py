"""W4 · the receipt⇄transaction join workflow (SPEC §6).

A short workflow per join attempt — one inbound receipt, or a re-check fired by
W1 when a fresh transaction posts. It runs the agentic ``match_receipt`` step,
then the pure :func:`~ynab_agent.join.match.plan_join` spine decides the single
action: signal the matched transaction's W2, ask one disambiguation question,
age the receipt out, or wait. The resulting store status is persisted so the
next re-check deduplicates (act once, ask once).
"""

from __future__ import annotations

from typing import assert_never

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from ynab_agent.join.match import (
        AskDisambiguation,
        AskNoMatch,
        DoNothing,
        JoinAction,
        Park,
        SignalTransaction,
        plan_join,
        resulting_status,
    )
    from ynab_agent.workflow import receipt_activities
    from ynab_agent.workflow.constants import ACTIVITY_TIMEOUT
    from ynab_agent.workflow.receipt_types import (
        ReceiptJoinParams,
        ReceiptJoinResult,
    )


@workflow.defn
class ReceiptJoinWorkflow:
    """One receipt-join attempt, matched then routed by the spine."""

    @workflow.run
    async def run(self, params: ReceiptJoinParams) -> ReceiptJoinResult:
        """Match the receipt and execute the single join action (SPEC §6)."""
        receipt = params.receipt
        outcome = await workflow.execute_activity(
            receipt_activities.match_receipt,
            receipt,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
        )
        action = plan_join(receipt, outcome, now=workflow.now())
        await self._execute(action)

        new_status = resulting_status(action)
        if new_status is not None:
            await workflow.execute_activity(
                receipt_activities.save_receipt_status,
                args=[str(receipt.id), new_status],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
            )
        return ReceiptJoinResult(
            action=action.kind, status=new_status or receipt.status
        )

    async def _execute(self, action: JoinAction) -> None:
        """Perform the one side effect the action calls for (SPEC §6)."""
        match action:
            case SignalTransaction(txn_id=txn_id, receipt_id=receipt_id):
                await workflow.execute_activity(
                    receipt_activities.signal_match,
                    args=[str(txn_id), str(receipt_id)],
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                )
                return
            case AskDisambiguation(receipt_id=receipt_id, candidates=cands):
                await workflow.execute_activity(
                    receipt_activities.ask_disambiguation,
                    args=[str(receipt_id), [str(c) for c in cands]],
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                )
                return
            case AskNoMatch(receipt_id=receipt_id):
                await workflow.execute_activity(
                    receipt_activities.ask_no_match,
                    str(receipt_id),
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                )
                return
            case Park() | DoNothing():
                return
        assert_never(action)
