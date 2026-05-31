"""OpenTelemetry wiring: a global tracer, SDK metrics, and instrumentation.

The one place the worker and the webhook turn telemetry on. Traces export
OTLP/HTTP; the Temporal SDK's runtime metrics export OTLP/gRPC via Temporal's
own Rust exporter (so no Python ``grpcio`` wheel is pulled). Both go to the
in-cluster collector ``temporal-otel-collector.temporal.svc.cluster.local`` —
no auth in-cluster; that collector adds the ClickStack ingest token on forward.

Everything here is gated on the ``YNAB_AGENT_OTEL`` flag and is a no-op when it
is unset, so tests and local runs stay completely telemetry-free (no background
exporter threads, no instrumentation side effects); the k8s Deployments set the
flag. It is never imported by a workflow- or activity-decorated module, so no
OpenTelemetry import enters the Temporal workflow sandbox graph.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx
    from fastapi import FastAPI
    from temporalio.client import Interceptor as ClientInterceptor
    from temporalio.runtime import Runtime

# In-cluster collector. Traces -> OTLP/HTTP :4318 (avoids the grpcio wheel);
# metrics -> OTLP/gRPC :4317 (Temporal's Rust exporter, also no python grpcio).
_COLLECTOR = "temporal-otel-collector.temporal.svc.cluster.local"
_ENABLED_ENV = "YNAB_AGENT_OTEL"
_TRACES_ENV = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
_METRICS_ENV = "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"

_tracing_configured = False


def otel_enabled() -> bool:
    """True when telemetry is switched on (the Deployments set the flag)."""
    return os.environ.get(_ENABLED_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _traces_endpoint() -> str:
    return os.environ.get(_TRACES_ENV, f"http://{_COLLECTOR}:4318/v1/traces")


def _metrics_endpoint() -> str:
    return os.environ.get(_METRICS_ENV, f"http://{_COLLECTOR}:4317")


def setup_tracing(service_name: str) -> None:
    """Install a global TracerProvider exporting OTLP/HTTP to the collector.

    Gated on ``YNAB_AGENT_OTEL`` and genuinely idempotent: the provider (and its
    one background export thread) is built at most once per process; later calls
    return early, so calling it from every ``create_app`` is safe.
    """
    global _tracing_configured
    if _tracing_configured or not otel_enabled():
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: service_name})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=_traces_endpoint()))
    )
    trace.set_tracer_provider(provider)
    _tracing_configured = True


def tracing_interceptors() -> Sequence[ClientInterceptor]:
    """The Temporal interceptors that carry trace context across the boundary.

    Set on ``Client.connect`` (they auto-apply to a Worker built from that
    client). ``always_create_workflow_spans=True`` so schedule-/cron-started
    workflows (the poll + overspend monitor) are traced too, not only those
    started under a client span. Empty when telemetry is off.
    """
    if not otel_enabled():
        return []
    from temporalio.contrib.opentelemetry import TracingInterceptor

    return [TracingInterceptor(always_create_workflow_spans=True)]


def metrics_runtime() -> Runtime | None:
    """A Temporal Runtime that pushes SDK metrics OTLP/gRPC, or ``None`` (off).

    ``None`` is accepted by ``Client.connect(runtime=...)`` (default runtime).
    """
    if not otel_enabled():
        return None
    from temporalio.runtime import (
        OpenTelemetryConfig,
        OpenTelemetryMetricTemporality,
        Runtime,
        TelemetryConfig,
    )

    return Runtime(
        telemetry=TelemetryConfig(
            metrics=OpenTelemetryConfig(
                url=_metrics_endpoint(),
                metric_temporality=OpenTelemetryMetricTemporality.DELTA,
                metric_periodicity=timedelta(seconds=30),
            )
        )
    )


def instrument_httpx(client: httpx.Client) -> None:
    """Instrument one httpx client so W3C traceparent rides outbound calls."""
    if not otel_enabled():
        return
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor.instrument_client(client)


def instrument_fastapi(app: FastAPI) -> None:
    """Instrument the webhook app so each request opens a server span."""
    if not otel_enabled():
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)
