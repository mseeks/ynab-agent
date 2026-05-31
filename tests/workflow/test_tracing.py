"""Tests for OpenTelemetry wiring (SPEC §0.6).

Two layers: (1) the ``ynab_agent.telemetry`` helpers are correctly gated on
``YNAB_AGENT_OTEL``, and (2) with the interceptor wired, a real workflow run
actually emits workflow + activity spans.

Module level stays sandbox-safe (only ``temporalio.*`` + the safe telemetry/
runtime modules + the trivial workflow), so the Temporal sandbox can import this
file to load ``PingWorkflow``; the OpenTelemetry SDK imports live inside the
test body, never at module import.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from temporalio import activity, workflow
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent import telemetry
from ynab_agent.workflow.runtime import DATA_CONVERTER

if TYPE_CHECKING:
    import pytest


@activity.defn
async def ping() -> str:
    return "pong"


@workflow.defn
class PingWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            ping, schedule_to_close_timeout=timedelta(seconds=10)
        )


def test_telemetry_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.delenv("YNAB_AGENT_OTEL", raising=False)
    assert telemetry.otel_enabled() is False
    assert list(telemetry.tracing_interceptors()) == []
    assert telemetry.metrics_runtime() is None
    # The instrumentation helpers are inert no-ops when disabled.
    telemetry.setup_tracing("test")
    telemetry.instrument_httpx(httpx.Client())


def test_flag_enables_the_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YNAB_AGENT_OTEL", "1")
    assert telemetry.otel_enabled() is True
    interceptors = list(telemetry.tracing_interceptors())
    assert len(interceptors) == 1
    assert isinstance(interceptors[0], TracingInterceptor)
    assert telemetry.metrics_runtime() is not None


async def test_workflow_run_emits_workflow_and_activity_spans() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    # Interceptor on the env client auto-applies to a Worker built from it.
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER,
            interceptors=[TracingInterceptor(tracer)],
        ) as env,
        Worker(
            env.client,
            task_queue="otel-test",
            workflows=[PingWorkflow],
            activities=[ping],
        ),
    ):
        # A client-originated parent span is required for the workflow span
        # (TracingInterceptor's always_create_workflow_spans defaults False).
        with tracer.start_as_current_span("start"):
            result = await env.client.execute_workflow(
                PingWorkflow.run, id="ping-otel", task_queue="otel-test"
            )

    assert result == "pong"
    names = {span.name for span in exporter.get_finished_spans()}
    assert any("RunWorkflow" in name for name in names)
    assert any("RunActivity" in name for name in names)
