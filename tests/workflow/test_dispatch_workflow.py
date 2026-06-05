"""End-to-end tests for the W3 dispatch workflow on the time-skipping server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.dispatch.classify import InboundKind, InboundMessage
from ynab_agent.domain.ids import MessageId, ThreadId
from ynab_agent.workflow.dispatch_types import DispatchParams, DispatchResult
from ynab_agent.workflow.dispatch_workflow import DispatchWorkflow
from ynab_agent.workflow.runtime import DATA_CONVERTER

if TYPE_CHECKING:
    from collections.abc import Callable

TASK_QUEUE = "ynab-dispatch-test"
_ALLOW = frozenset({"matthew@example.com"})


def _msg(
    *,
    thread: str | None = None,
    verified: bool = True,
    sender: str = "matthew@example.com",
) -> InboundMessage:
    return InboundMessage(
        message_id=MessageId("m1"),
        from_address=sender,
        subject="[YNAB] $4.50",
        body="ok",
        thread_id=ThreadId(thread) if thread is not None else None,
        signature_verified=verified,
    )


def _activities(
    *,
    resolve: str | None,
    resolve_offer: str | None,
    classify_kind: InboundKind,
    calls: list[tuple[str, str]],
) -> list[Callable[..., object]]:
    @activity.defn(name="resolve_thread")
    async def resolve_thread(thread_id: str | None) -> str | None:
        return resolve

    @activity.defn(name="resolve_offer_thread")
    async def resolve_offer_thread(thread_id: str | None) -> str | None:
        return resolve_offer

    @activity.defn(name="classify_inbound")
    async def classify_inbound(message: InboundMessage) -> InboundKind:
        return classify_kind

    @activity.defn(name="signal_transaction")
    async def signal_transaction(txn_id: str, message: InboundMessage) -> None:
        calls.append(("signal", txn_id))

    @activity.defn(name="signal_offer")
    async def signal_offer(offer_id: str, message: InboundMessage) -> None:
        calls.append(("offer", offer_id))

    @activity.defn(name="route_receipt")
    async def route_receipt(message: InboundMessage) -> None:
        calls.append(("receipt", message.message_id))

    @activity.defn(name="handle_command")
    async def handle_command(message: InboundMessage) -> None:
        calls.append(("command", message.message_id))

    return [
        resolve_thread,
        resolve_offer_thread,
        classify_inbound,
        signal_transaction,
        signal_offer,
        route_receipt,
        handle_command,
    ]


async def _run(
    *,
    wf_id: str,
    message: InboundMessage,
    resolve: str | None = None,
    resolve_offer: str | None = None,
    classify_kind: InboundKind = InboundKind.NOISE,
) -> tuple[DispatchResult, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []
    acts = _activities(
        resolve=resolve,
        resolve_offer=resolve_offer,
        classify_kind=classify_kind,
        calls=calls,
    )
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DispatchWorkflow],
            activities=acts,
        ),
    ):
        result = await env.client.execute_workflow(
            DispatchWorkflow.run,
            DispatchParams(message=message, allowlist=_ALLOW),
            id=wf_id,
            task_queue=TASK_QUEUE,
        )
    return result, calls


async def test_reply_on_thread_signals_transaction() -> None:
    result, calls = await _run(
        wf_id="d-reply", message=_msg(thread="thr1"), resolve="t1"
    )
    assert result.action == "transaction"
    assert ("signal", "t1") in calls


async def test_reply_on_offer_thread_signals_offer() -> None:
    # Not a transaction thread, but a live autonomy-offer thread → the reply is
    # a bless-acceptance, routed to that offer workflow (SPEC §14.7 3b).
    result, calls = await _run(
        wf_id="d-offer",
        message=_msg(thread="thr-offer"),
        resolve=None,
        resolve_offer="autonomy-offer-r1",
    )
    assert result.action == "offer"
    assert ("offer", "autonomy-offer-r1") in calls


async def test_forwarded_receipt_routes_to_join() -> None:
    result, calls = await _run(
        wf_id="d-receipt",
        message=_msg(),
        resolve=None,
        classify_kind=InboundKind.RECEIPT,
    )
    assert result.action == "receipt"
    assert ("receipt", "m1") in calls


async def test_unsigned_message_quarantined() -> None:
    result, calls = await _run(wf_id="d-quar", message=_msg(verified=False))
    assert result.action == "quarantine"
    assert calls == []
