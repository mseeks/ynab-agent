"""The I/O ports of the W6 overspend monitor, as Temporal activities.

Its own module so the monitor workflow's sandbox import graph stays minimal
(see ``poll_activities`` / ``dispatch_activities``). Heavy clients (YNAB,
AgentMail, the Temporal client) are imported lazily inside the bodies so they
never enter the workflow sandbox.

The per-period dedupe store is the durable
:class:`~ynab_agent.workflow.overspend_ledger_workflow.OverspendLedgerWorkflow`:
``load_prior_alert`` queries it and ``save_alert`` signals it, exactly as the
W2 activities talk to the rule registry and the failure-alert ledger. The
``period`` (``"YYYY-MM"``) is computed once from the workflow's deterministic
clock and passed *into* every activity in a pass, so they can never disagree on
it across a month boundary (the thread, the dedupe key, and the W7 offer id all
derive from it). "Days left" is cosmetic and still read at send time.
"""

from __future__ import annotations

import calendar
import datetime

from temporalio import activity

from ynab_agent.budget.overspend import (
    CategorySpend,
    OverspendAssessment,
    PriorAlert,
)
from ynab_agent.domain.config import HOUSEHOLD_TZ
from ynab_agent.workflow.monitor_types import PeriodClock


def _days_left_in_month(now: datetime.datetime) -> int:
    """Calendar days remaining in ``now``'s month (0 on the last day)."""
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    return days_in_month - now.day


@activity.defn
async def current_period() -> PeriodClock:
    """The budget period + month position, in household time (SPEC §7, §13).

    The timezone conversion (``period_and_clock``) runs here, in an activity,
    so the workflow sandbox never touches ``zoneinfo``; the recorded result
    keeps the workflow replay-deterministic, and one period reaches every
    activity in the pass.
    """
    from ynab_agent.budget.overspend import period_and_clock

    period, clock = period_and_clock(datetime.datetime.now(datetime.UTC))
    return PeriodClock(period=period, clock=clock)


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
async def load_prior_alert(category_id: str, period: str) -> PriorAlert | None:
    """Load the last alert raised for a category this period, for dedupe.

    Queries the durable ledger for ``period`` (supplied by the workflow from its
    deterministic clock, so it matches the period every other activity in the
    pass uses). Returns ``None`` when the ledger has not been started yet
    (nothing has ever alerted) — so the first flag of a period always alerts
    (SPEC §7).

    The client-side query decodes a pydantic model to a plain ``dict``, so the
    result is hydrated back into a :class:`PriorAlert` here — ``should_alert``
    reads attributes off it (the #4 dict-vs-object class of bug). We hydrate by
    hand rather than via ``result_type`` because the result is optional and the
    SDK types ``result_type`` as a plain ``type``, not a ``X | None`` union.
    """
    from temporalio.service import RPCError

    from ynab_agent.workflow.overspend_ledger_types import (
        OVERSPEND_LEDGER_WORKFLOW_ID,
        PriorRequest,
    )
    from ynab_agent.workflow.temporal_client import client

    temporal = await client()
    handle = temporal.get_workflow_handle(OVERSPEND_LEDGER_WORKFLOW_ID)
    try:
        raw = await handle.query(
            "prior", PriorRequest(category=category_id, period=period)
        )
    except RPCError:
        return None
    return None if raw is None else PriorAlert.model_validate(raw)


@activity.defn
async def send_overspend_alert(
    assessment: OverspendAssessment, period: str
) -> str:
    """Email the overspend alert and return its thread id (SPEC §7, §8).

    The body is deterministic (no model): the figures and verdict are already
    decided. ``period`` is supplied by the workflow (from its deterministic
    clock), so the thread and dedupe labels match the rest of the pass.
    ``alert_on_thread`` keeps one thread per overspend: the first alert opens
    it, a *worsening* re-alert (the one ``should_alert`` admits mid-period)
    replies an update on that same thread, and a retry of either re-sends
    nothing (idempotent on the update label). The id it returns is stable for
    the period: what W7 replies on to offer a balancing move, and where a
    worsening re-alert lands too, so the conversation never forks (the W6→W7
    tie).
    """
    import asyncio

    from ynab_agent.budget.message import (
        overspend_body,
        overspend_subject,
        overspend_thread_label,
        overspend_update_label,
    )
    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings

    # "Days left" is the household's, not UTC's (SPEC §13): near midnight the
    # two differ by a day, and the owner reads this in their own evening.
    now = datetime.datetime.now(datetime.UTC).astimezone(HOUSEHOLD_TZ)
    settings = Settings()
    mail = MailClient.from_env()
    return await asyncio.to_thread(
        mail.alert_on_thread,
        inbox_id=settings.inbox,
        to=list(settings.owners),
        subject=overspend_subject(assessment),
        body=overspend_body(assessment, _days_left_in_month(now)),
        thread_label=overspend_thread_label(assessment, period),
        update_label=overspend_update_label(assessment, period),
    )


@activity.defn
async def save_alert(category_id: str, alert: PriorAlert, period: str) -> None:
    """Record this period's alert so the next pass can dedupe against it.

    Signal-with-start on the singleton ledger: the first alert creates it, every
    later one just delivers the signal (SPEC §7) — the same shape as
    ``feed_rule_learning`` and the failure-alert ledger. ``period`` is supplied
    by the workflow so the record matches what ``load_prior_alert`` queried.
    """
    from temporalio.common import WorkflowIDConflictPolicy

    from ynab_agent.workflow.overspend_ledger_types import (
        OVERSPEND_LEDGER_WORKFLOW_ID,
        LedgerParams,
        RecordRequest,
    )
    from ynab_agent.workflow.temporal_client import client, task_queue

    temporal = await client()
    await temporal.start_workflow(
        "OverspendLedgerWorkflow",
        LedgerParams(),
        id=OVERSPEND_LEDGER_WORKFLOW_ID,
        task_queue=task_queue(),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="record",
        start_signal_args=[
            RecordRequest(category=category_id, period=period, alert=alert)
        ],
    )
