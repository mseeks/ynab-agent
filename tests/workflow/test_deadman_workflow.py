"""The §13 deadman: probe the poll loop, page on a problem (out-of-band).

The workflow is exercised on the time-skipping server with mock activities;
the probe itself (``check_poll_liveness``) is exercised against a fake Temporal
client covering the three unhealthy shapes — missing, stopped, and wedged —
plus the healthy one.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import TYPE_CHECKING

from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.workflow import poll_activities, temporal_client
from ynab_agent.workflow.alert_types import FailureAlert
from ynab_agent.workflow.deadman_workflow import DeadmanWorkflow
from ynab_agent.workflow.runtime import DATA_CONVERTER

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

_TASK_QUEUE = "deadman-test"


def _activities(
    *, problem: str | None, pages: list[str]
) -> list[Callable[..., object]]:
    @activity.defn(name="check_poll_liveness")
    async def check_poll_liveness() -> str | None:
        return problem

    @activity.defn(name="alert_failure")
    async def alert_failure(alert: FailureAlert) -> None:
        pages.append(alert.body)

    return [check_poll_liveness, alert_failure]


async def _run(problem: str | None) -> tuple[str, list[str]]:
    pages: list[str] = []
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[DeadmanWorkflow],
            activities=_activities(problem=problem, pages=pages),
        ),
    ):
        result = await env.client.execute_workflow(
            DeadmanWorkflow.run,
            id="deadman-test",
            task_queue=_TASK_QUEUE,
        )
    return result, pages


async def test_healthy_poll_pages_nothing() -> None:
    result, pages = await _run(None)
    assert result == "ok"
    assert pages == []


async def test_problem_pages_with_the_description() -> None:
    result, pages = await _run("the W1 poll loop is TERMINATED")
    assert "TERMINATED" in result
    assert pages == ["the W1 poll loop is TERMINATED"]


# ── the probe itself, against a fake client ──────────────────────────────────
class _FakeHandle:
    def __init__(self, description: object | None) -> None:
        self._description = description

    async def describe(self) -> object:
        if self._description is None:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        return self._description


class _FakeDescription:
    def __init__(
        self, status: WorkflowExecutionStatus, age: datetime.timedelta
    ) -> None:
        self.status = status
        self.start_time = datetime.datetime.now(datetime.UTC) - age


class _FakeClient:
    def __init__(self, description: object | None) -> None:
        self._description = description

    def get_workflow_handle(self, workflow_id: str) -> _FakeHandle:
        assert workflow_id == poll_activities.POLL_WORKFLOW_ID
        return _FakeHandle(self._description)


def _probe(
    monkeypatch: pytest.MonkeyPatch, description: object | None
) -> str | None:
    monkeypatch.setattr(temporal_client, "_CLIENT", _FakeClient(description))
    return asyncio.run(poll_activities.check_poll_liveness())


def test_probe_flags_a_missing_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    problem = _probe(monkeypatch, None)
    assert problem is not None
    assert "does not exist" in problem


def test_probe_flags_a_stopped_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    description = _FakeDescription(
        WorkflowExecutionStatus.TERMINATED, datetime.timedelta(minutes=5)
    )
    problem = _probe(monkeypatch, description)
    assert problem is not None
    assert "TERMINATED" in problem


def test_probe_flags_a_wedged_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # Running, but the current run started far past the stale horizon — the
    # loop continues-as-new each tick, so an old run means no recent tick.
    description = _FakeDescription(
        WorkflowExecutionStatus.RUNNING, datetime.timedelta(hours=7)
    )
    problem = _probe(monkeypatch, description)
    assert problem is not None
    assert "wedged" in problem


def test_probe_passes_a_fresh_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    description = _FakeDescription(
        WorkflowExecutionStatus.RUNNING, datetime.timedelta(minutes=20)
    )
    assert _probe(monkeypatch, description) is None
