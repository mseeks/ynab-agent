"""The Temporal worker entrypoint — the runnable assembly (SPEC §0.5).

Connects to Temporal with the Pydantic data converter and registers the whole
runtime: every workflow and every activity port. The activities are
progressively wired to the real clients (YNAB reads are live; the model and mail
sends land as each is connected); registering them all here is the single place
a deployment turns on.

Run it once a Temporal server is reachable::

    python -m ynab_agent.worker            # localhost:7233, queue "ynab-agent"

Environment: the clients read their own keys (``YNAB_API_KEY``,
``AGENTMAIL_API_KEY``) and :class:`~ynab_agent.settings.Settings` reads the
non-secret config; none of it is in the repo.
"""

from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from ynab_agent.workflow.runtime import (
    ALL_ACTIVITIES,
    DATA_CONVERTER,
    WORKFLOWS,
)

DEFAULT_HOST = "localhost:7233"
DEFAULT_TASK_QUEUE = "ynab-agent"


async def run_worker(
    *,
    target_host: str = DEFAULT_HOST,
    task_queue: str = DEFAULT_TASK_QUEUE,
) -> None:
    """Connect to Temporal and run the worker until cancelled (SPEC §0.5)."""
    client = await Client.connect(
        target_host, data_converter=DATA_CONVERTER
    )
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=WORKFLOWS,
        activities=ALL_ACTIVITIES,
    )
    await worker.run()


def main() -> None:
    """Console entrypoint: run the worker against the default Temporal."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
