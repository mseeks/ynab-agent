"""The Temporal worker entrypoint — the runnable assembly (SPEC §0.5).

Connects to Temporal with the Pydantic data converter and registers the whole
runtime: every workflow and every activity port. The activities are
progressively wired to the real clients (YNAB reads are live; the model and mail
sends land as each is connected); registering them all here is the single place
a deployment turns on.

Run it once a Temporal server is reachable::

    python -m ynab_agent.worker

The connection is env-configured so the same image runs anywhere:
``TEMPORAL_HOST`` (default ``localhost:7233``), ``TEMPORAL_NAMESPACE`` (default
``default``), and ``TEMPORAL_TASK_QUEUE`` (default ``ynab-agent``). The clients
read their own keys (``YNAB_API_KEY``, ``AGENTMAIL_API_KEY``), the model
endpoint (``YNAB_AGENT_OLLAMA_URL`` / ``YNAB_AGENT_MODEL``), and ``Settings``
reads the recipient config — all from the environment, never the repo.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

from temporalio.client import Client
from temporalio.worker import Worker

from ynab_agent.telemetry import (
    metrics_runtime,
    setup_tracing,
    shutdown_tracing,
    tracing_interceptors,
)
from ynab_agent.workflow.runtime import (
    ALL_ACTIVITIES,
    DATA_CONVERTER,
    WORKFLOWS,
)

DEFAULT_HOST = "localhost:7233"
DEFAULT_NAMESPACE = "default"
DEFAULT_TASK_QUEUE = "ynab-agent"


async def run_worker(
    *,
    target_host: str | None = None,
    namespace: str | None = None,
    task_queue: str | None = None,
) -> None:
    """Connect to Temporal and run the worker until cancelled (SPEC §0.5).

    Each parameter falls back to its ``TEMPORAL_*`` environment variable, then
    to the in-cluster default, so a deployment configures the worker purely
    through the environment.
    """
    host = target_host or os.environ.get("TEMPORAL_HOST", DEFAULT_HOST)
    ns = namespace or os.environ.get("TEMPORAL_NAMESPACE", DEFAULT_NAMESPACE)
    queue = task_queue or os.environ.get(
        "TEMPORAL_TASK_QUEUE", DEFAULT_TASK_QUEUE
    )
    setup_tracing("ynab-agent-worker")
    client = await Client.connect(
        host,
        namespace=ns,
        data_converter=DATA_CONVERTER,
        interceptors=tracing_interceptors(),
        runtime=metrics_runtime(),
    )
    worker = Worker(
        client,
        task_queue=queue,
        workflows=WORKFLOWS,
        activities=ALL_ACTIVITIES,
    )
    # Run until SIGTERM/SIGINT, then shut down gracefully and flush telemetry.
    # `uv run` forwards SIGTERM to us, and Python's atexit does NOT run on an
    # unhandled signal — so without this the worker's last span batch is dropped
    # on every (Recreate) rollout.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        async with worker:
            await stop.wait()
    finally:
        shutdown_tracing()


def main() -> None:
    """Console entrypoint: run the worker, configured from the environment."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
