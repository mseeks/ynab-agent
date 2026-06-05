"""Run the dashboard standalone for local development.

Usage::

    python -m ynab_agent.dashboard

Connects its own Temporal client from the ``TEMPORAL_*`` environment (point it
at a ``kubectl port-forward`` of the frontend) and serves until interrupted. In
production the worker hosts the same server in-process — this entrypoint is only
for developing or eyeballing the page against live data from a laptop.
"""

from __future__ import annotations

import asyncio
import contextlib
import os


async def _serve() -> None:
    from temporalio.client import Client

    from ynab_agent.dashboard.server import start
    from ynab_agent.workflow.runtime import DATA_CONVERTER

    client = await Client.connect(
        os.environ.get("TEMPORAL_HOST", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        data_converter=DATA_CONVERTER,
    )
    server = await start(client)
    async with server:
        await server.serve_forever()


def main() -> None:
    """Console entrypoint: serve the dashboard until Ctrl-C."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve())


if __name__ == "__main__":
    main()
