"""Start (once) the durable W1 ingestion poll loop (SPEC §2, §13).

Run as a one-shot after the worker is up (a k8s Job, or ``python -m
ynab_agent.poll_starter`` locally). It submits the single long-lived
``PollWorkflow`` — workflow id ``ynab-poll``, ``REJECT_DUPLICATE`` so it stays a
singleton. Each tick reads YNAB's unapproved set (the outstanding work), so the
current outstanding transactions surface as emails right away; the loop then
continues-as-new (no cursor — the set is derived from YNAB, SPEC §0.5).

Config from the environment: ``TEMPORAL_HOST`` / ``TEMPORAL_NAMESPACE`` /
``TEMPORAL_TASK_QUEUE``, the ``YNAB_BUDGET_ID``, an optional
``YNAB_AGENT_INSTALL_DATE`` (ISO date — the ingest cutover that bounds the scope
by date; defaults to ~90 days back) and ``YNAB_AGENT_POLL_INTERVAL_SECONDS``
(seconds between ticks; default 3600). Re-running is safe: an already-started
loop is left untouched.
"""

from __future__ import annotations

import asyncio
import datetime
import os

_POLL_ID = "ynab-poll"
_DEFAULT_WINDOW_DAYS = 90


def _resolve_install_date(
    value: str | None, *, today: datetime.date
) -> datetime.date:
    """The ingest cutover: the configured ISO date, else ~90 days back."""
    if value:
        return datetime.date.fromisoformat(value)
    return today - datetime.timedelta(days=_DEFAULT_WINDOW_DAYS)


async def _start() -> None:
    from temporalio.client import Client
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from ynab_agent.ingest.scope import IngestScope
    from ynab_agent.workflow.poll_types import PollParams
    from ynab_agent.workflow.poll_workflow import PollWorkflow

    install_date = _resolve_install_date(
        os.environ.get("YNAB_AGENT_INSTALL_DATE"),
        today=datetime.datetime.now(datetime.UTC).date(),
    )
    params = PollParams(
        scope=IngestScope(
            budget_id=os.environ.get("YNAB_BUDGET_ID", "last-used"),
            install_date=install_date,
        ),
        continuous=True,
        interval_seconds=int(
            os.environ.get("YNAB_AGENT_POLL_INTERVAL_SECONDS", "3600")
        ),
    )
    client = await Client.connect(
        os.environ.get("TEMPORAL_HOST", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        data_converter=pydantic_data_converter,
    )
    try:
        handle = await client.start_workflow(
            PollWorkflow.run,
            params,
            id=_POLL_ID,
            task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", "ynab-agent"),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        print(f"poll loop {_POLL_ID!r} already running — nothing to do")
        return
    print(
        f"started poll loop {handle.id!r} "
        f"(install_date={install_date}, cursor=0, continuous)"
    )


def main() -> None:
    """Console entrypoint: start the poll loop, configured from the env."""
    asyncio.run(_start())


if __name__ == "__main__":
    main()
