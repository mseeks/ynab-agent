"""Tests for the W3 dispatch activities' pure logic (SPEC §5).

``resolve_thread`` maps an AgentMail thread id back to its transaction via a
Temporal visibility query on the ``TxnThreadId`` search attribute (store-free,
SPEC §0.5). The query + extraction logic is exercised here against a fake client
injected as the cached connection; the live visibility query is covered by the
worker against a real Temporal server.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ynab_agent.workflow import dispatch_activities, temporal_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest


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
