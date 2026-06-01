"""A lazily-connected, process-wide Temporal client for activities.

A few activities must reach Temporal itself: W1 ingest starts a transaction
workflow per new YNAB transaction, and W3 dispatch resolves a reply's thread to
its workflow through a visibility query. Both need a client, so this connects
one per process on first use and caches it (like the YNAB/AgentMail clients).

Telemetry is intentionally NOT imported here: this module sits in the activity
import graph, which the workflow sandbox keeps free of OpenTelemetry (see the
``telemetry`` module docstring). SDK metrics for these occasional client calls
do not justify pulling the OTel stack across the sandbox boundary.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from temporalio.client import Client

# Connected on first use; reset by ``tests/conftest.py`` between tests.
_CLIENT: Client | None = None


def task_queue() -> str:
    """The task queue the workers listen on (env-configured)."""
    return os.environ.get("TEMPORAL_TASK_QUEUE", "ynab-agent")


async def _connect() -> Client:
    from temporalio.client import Client
    from temporalio.contrib.pydantic import pydantic_data_converter

    return await Client.connect(
        os.environ.get("TEMPORAL_HOST", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        data_converter=pydantic_data_converter,
    )


async def client() -> Client:
    """The process-wide Temporal client, connected on first use."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = await _connect()
    return _CLIENT
