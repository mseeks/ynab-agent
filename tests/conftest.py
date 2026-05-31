"""Shared fixtures: reset process-wide singletons between tests.

The YNAB + AgentMail clients cache a ``from_env()`` singleton, the telemetry
module caches whether tracing was configured, and OpenTelemetry's global tracer
provider is set-once — all process-global, so wipe them around every test for
isolation (otherwise one test's instrumented client / provider leaks into the
next).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

import ynab_agent.mail.client as mail_client
import ynab_agent.telemetry as telemetry
import ynab_agent.ynab.client as ynab_client

if TYPE_CHECKING:
    from collections.abc import Iterator


def _reset() -> None:
    ynab_client._CACHED = None
    mail_client._CACHED = None
    telemetry._tracing_configured = False
    # Clear OTel's set-once global tracer provider so a test can install one.
    with contextlib.suppress(Exception):
        from opentelemetry import trace
        from opentelemetry.util._once import Once

        trace.__dict__["_TRACER_PROVIDER_SET_ONCE"] = Once()
        trace.__dict__["_TRACER_PROVIDER"] = None


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Reset module-global caches/telemetry/tracer-provider around each test."""
    _reset()
    yield
    _reset()
