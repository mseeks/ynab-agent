"""Tests for the W3 dispatch activities (SPEC §5).

``resolve_thread`` maps an AgentMail thread id back to its transaction via a
Temporal visibility query on the ``TxnThreadId`` search attribute (store-free,
SPEC §0.5); ``signal_transaction`` turns a verified reply into a
``submit_inbound`` signal-with-start on that W2. Both are exercised against a
fake client injected as the cached connection; the live calls are covered by
the worker against a real Temporal server.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from ynab_agent.dispatch.classify import InboundMessage
from ynab_agent.domain.ids import MessageId, ThreadId
from ynab_agent.workflow import dispatch_activities, temporal_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _FakeExecution:
    def __init__(self, workflow_id: str) -> None:
        self.id = workflow_id


class _FakeClient:
    """Stands in for the Temporal client's ``list_workflows`` visibility API."""

    def __init__(self, executions: list[_FakeExecution]) -> None:
        self._executions = executions
        self.queries: list[str] = []

    def list_workflows(self, query: str) -> AsyncIterator[_FakeExecution]:
        self.queries.append(query)

        async def _gen() -> AsyncIterator[_FakeExecution]:
            for execution in self._executions:
                yield execution

        return _gen()


def test_resolve_thread_returns_matching_workflow_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient([_FakeExecution("txn-123")])
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    result = asyncio.run(dispatch_activities.resolve_thread("thread-abc"))
    assert result == "txn-123"
    assert fake.queries == ['TxnThreadId = "thread-abc"']


def test_resolve_thread_none_when_no_workflow_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient([])
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    assert asyncio.run(dispatch_activities.resolve_thread("orphan")) is None


def test_resolve_thread_none_for_none_input() -> None:
    # No client is touched when there is no thread id to resolve.
    assert asyncio.run(dispatch_activities.resolve_thread(None)) is None


def test_resolve_thread_escapes_quotes_in_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient([_FakeExecution("txn-9")])
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    asyncio.run(dispatch_activities.resolve_thread('th"read'))
    assert fake.queries == ['TxnThreadId = "th\\"read"']


def test_resolve_offer_thread_query_filters_to_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient([_FakeExecution("autonomy-offer-r1")])
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    result = asyncio.run(dispatch_activities.resolve_offer_thread("thread-abc"))
    assert result == "autonomy-offer-r1"
    # A closed offer must not be resurrected, so the query is Running-only.
    assert fake.queries == [
        'OfferThreadId = "thread-abc" AND ExecutionStatus = "Running"'
    ]


def test_resolve_offer_thread_none_for_none_input() -> None:
    assert asyncio.run(dispatch_activities.resolve_offer_thread(None)) is None


class _FakeHandle:
    def __init__(self) -> None:
        self.signals: list[tuple[str, object]] = []

    async def signal(self, name: str, arg: object) -> None:
        self.signals.append((name, arg))


class _FakeHandleClient:
    """Stands in for ``get_workflow_handle`` + ``signal``."""

    def __init__(self) -> None:
        self.handle = _FakeHandle()
        self.requested: list[str] = []

    def get_workflow_handle(self, workflow_id: str) -> _FakeHandle:
        self.requested.append(workflow_id)
        return self.handle


def test_signal_offer_signals_the_offer_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeHandleClient()
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    asyncio.run(
        dispatch_activities.signal_offer(
            "autonomy-offer-r1", _message(thread_id="t")
        )
    )
    assert fake.requested == ["autonomy-offer-r1"]
    assert len(fake.handle.signals) == 1
    name, arg = fake.handle.signals[0]
    assert name == "submit_response"
    assert arg.body == "actually make it Dining"  # type: ignore[attr-defined]


class _FakeStartClient:
    """Captures ``start_workflow`` (signal-with-start) calls."""

    def __init__(self) -> None:
        self.started: list[tuple[object, object, dict[str, object]]] = []

    async def start_workflow(
        self, workflow: object, arg: object, **kwargs: object
    ) -> None:
        self.started.append((workflow, arg, kwargs))


def _message(*, thread_id: str | None) -> InboundMessage:
    return InboundMessage(
        message_id=MessageId("m1"),
        from_address="owner@example.com",
        subject="re: coffee",
        body="actually make it Dining",
        thread_id=ThreadId(thread_id) if thread_id is not None else None,
        signature_verified=True,
    )


def test_signal_transaction_signals_with_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeStartClient()
    monkeypatch.setattr(temporal_client, "_CLIENT", fake)
    asyncio.run(
        dispatch_activities.signal_transaction("txn-1", _message(thread_id="t"))
    )
    assert len(fake.started) == 1
    workflow, _arg, kwargs = fake.started[0]
    assert workflow == "TransactionWorkflow"
    assert kwargs["id"] == "txn-1"
    assert kwargs["start_signal"] == "submit_inbound"
    reply = kwargs["start_signal_args"][0]  # type: ignore[index]
    assert reply.text == "actually make it Dining"
    assert reply.thread_id == "t"


def test_signal_transaction_requires_a_thread_id() -> None:
    with pytest.raises(RuntimeError, match="no thread id"):
        asyncio.run(
            dispatch_activities.signal_transaction(
                "txn-1", _message(thread_id=None)
            )
        )
