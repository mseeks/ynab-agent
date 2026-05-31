"""The I/O ports of the W6 overspend monitor, as Temporal activities.

Its own module so the monitor workflow's sandbox import graph stays minimal
(see ``poll_activities`` / ``dispatch_activities``). All stubbed; the YNAB read,
the AgentMail alert, and the per-period alert store are wired later.
"""

from __future__ import annotations

from temporalio import activity

from ynab_agent.budget.overspend import (
    CategorySpend,
    OverspendAssessment,
    PriorAlert,
)

_STUB = "workflow activity stub — register a real or mock implementation"


@activity.defn
async def fetch_category_spends() -> list[CategorySpend]:
    """Read each category's month-to-date budget figures from YNAB (§7).

    The YNAB client is imported lazily and its blocking call runs off the loop.
    """
    import asyncio

    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    return list(await asyncio.to_thread(client.category_spends))


@activity.defn
async def load_prior_alert(category_id: str) -> PriorAlert | None:
    """Load the last alert raised for a category this period, for dedupe."""
    raise NotImplementedError(_STUB)


@activity.defn
async def send_overspend_alert(assessment: OverspendAssessment) -> None:
    """Email the overspend alert on its own thread (SPEC §7, notify-only)."""
    raise NotImplementedError(_STUB)


@activity.defn
async def save_alert(category_id: str, alert: PriorAlert) -> None:
    """Record this period's alert so the next pass can dedupe against it."""
    raise NotImplementedError(_STUB)
