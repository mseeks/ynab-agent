"""End-to-end tests for the W4 receipt-join workflow (time-skipping server)."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.domain.enums import ReceiptStatus
from ynab_agent.domain.ids import ReceiptId, YnabTransactionId
from ynab_agent.domain.receipt import Receipt
from ynab_agent.join.match import (
    Ambiguous,
    ConfidentMatch,
    MatchOutcome,
    NoMatch,
)
from ynab_agent.workflow.receipt_activities import match_receipt
from ynab_agent.workflow.receipt_types import (
    ReceiptJoinParams,
    ReceiptJoinResult,
)
from ynab_agent.workflow.receipt_workflow import ReceiptJoinWorkflow
from ynab_agent.workflow.runtime import DATA_CONVERTER

if TYPE_CHECKING:
    from collections.abc import Callable

TASK_QUEUE = "ynab-receipt-test"
# Far future / far past pin the TTL branch regardless of the server clock.
_FRESH = datetime.datetime(2099, 1, 1, tzinfo=datetime.UTC)
_STALE = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
_T1 = YnabTransactionId("t1")
_T2 = YnabTransactionId("t2")


def _receipt(
    *,
    parked: datetime.datetime = _FRESH,
    status: ReceiptStatus = ReceiptStatus.PARKED,
) -> Receipt:
    return Receipt(id=ReceiptId("r1"), parked_at=parked, status=status)


def _activities(
    *, outcome: MatchOutcome, calls: list[tuple[str, str]]
) -> list[Callable[..., object]]:
    @activity.defn(name="match_receipt")
    async def match_receipt_mock(receipt: Receipt) -> MatchOutcome:
        return outcome

    @activity.defn(name="signal_match")
    async def signal_match(txn_id: str, receipt_id: str) -> None:
        calls.append(("signal", txn_id))

    @activity.defn(name="ask_disambiguation")
    async def ask_disambiguation(
        receipt_id: str, candidates: list[str]
    ) -> None:
        calls.append(("ask", ",".join(candidates)))

    @activity.defn(name="ask_no_match")
    async def ask_no_match(receipt_id: str) -> None:
        calls.append(("no_match", receipt_id))

    @activity.defn(name="save_receipt_status")
    async def save_receipt_status(
        receipt_id: str, status: ReceiptStatus
    ) -> None:
        calls.append(("save", status.value))

    return [
        match_receipt_mock,
        signal_match,
        ask_disambiguation,
        ask_no_match,
        save_receipt_status,
    ]


async def _run(
    *, wf_id: str, receipt: Receipt, outcome: MatchOutcome
) -> tuple[ReceiptJoinResult, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []
    acts = _activities(outcome=outcome, calls=calls)
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ReceiptJoinWorkflow],
            activities=acts,
        ),
    ):
        result = await env.client.execute_workflow(
            ReceiptJoinWorkflow.run,
            ReceiptJoinParams(receipt=receipt),
            id=wf_id,
            task_queue=TASK_QUEUE,
        )
    return result, calls


def test_match_receipt_is_an_activity() -> None:
    # The agentic match runs as an activity, never in workflow code.
    assert hasattr(match_receipt, "__temporal_activity_definition")


async def test_confident_match_signals_and_marks_matched() -> None:
    result, calls = await _run(
        wf_id="r-confident",
        receipt=_receipt(),
        outcome=ConfidentMatch(txn_id=_T1),
    )
    assert result.action == "signal"
    assert result.status is ReceiptStatus.MATCHED
    assert ("signal", "t1") in calls
    assert ("save", "matched") in calls


async def test_ambiguous_asks_and_marks_asked() -> None:
    result, calls = await _run(
        wf_id="r-ambiguous",
        receipt=_receipt(),
        outcome=Ambiguous(candidates=(_T1, _T2)),
    )
    assert result.action == "ask_disambiguation"
    assert result.status is ReceiptStatus.ASKED
    assert ("ask", "t1,t2") in calls


async def test_no_match_within_ttl_parks_and_saves_nothing() -> None:
    result, calls = await _run(
        wf_id="r-park", receipt=_receipt(parked=_FRESH), outcome=NoMatch()
    )
    assert result.action == "park"
    assert result.status is ReceiptStatus.PARKED
    assert calls == []


async def test_no_match_past_ttl_ages_out() -> None:
    result, calls = await _run(
        wf_id="r-expire", receipt=_receipt(parked=_STALE), outcome=NoMatch()
    )
    assert result.action == "ask_no_match"
    assert result.status is ReceiptStatus.EXPIRED
    assert ("no_match", "r1") in calls
    assert ("save", "expired") in calls
