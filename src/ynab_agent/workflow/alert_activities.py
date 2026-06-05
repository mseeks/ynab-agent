"""The failure-alert activity: a deduped ntfy push, best-effort (SPEC §13).

``alert_failure`` runs from the W2 terminal-failure hook *while a transaction is
already failing*, so its prime directive is: **never become a new failure.**
Every error here is logged and swallowed — a missed page is acceptable; an alert
path that raises (masking the real failure) or loops is not. Because it never
raises, Temporal sees success and never retries it, so the push happens at most
once per hook invocation.

The dedup + rate cap live in the durable ``AlertLedgerWorkflow``: this activity
asks it ``should_notify`` before sending and signals ``record`` after, exactly
as ``feed_rule_learning`` talks to the rule registry. Heavy imports
(``httpx`` via the notify client) stay lazy so this module — referenced from the
workflow sandbox — never pulls them across the boundary.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from temporalio import activity

from ynab_agent.workflow.alert_types import FailureAlert

if TYPE_CHECKING:
    from ynab_agent.notify.client import Notification, NotifyClient


@activity.defn
async def alert_failure(alert: FailureAlert) -> None:
    """Push one deduped operator alert. Best-effort: never raises."""
    import asyncio

    try:
        now = datetime.datetime.now(datetime.UTC)
        if not await _should_notify(alert.key, now):
            activity.logger.info(
                "failure alert suppressed (dedup/rate cap): %s", alert.key
            )
            return
        client = _client_or_none()
        if client is None:
            activity.logger.warning(
                "NTFY_TOPIC unset; failure alert dropped: %s", alert.key
            )
            return
        # The ntfy POST is blocking; keep it off the event loop. If it raises
        # (ntfy down), the outer guard logs it and we do *not* record — so a
        # later failure can try again rather than being deduped into silence.
        await asyncio.to_thread(client.notify, _build_notification(alert))
        await _record(alert.key)
    except Exception:
        # Best-effort: never let an alerting failure mask the real one.
        activity.logger.warning(
            "failure alert could not be delivered", exc_info=True
        )


async def _should_notify(key: str, now: datetime.datetime) -> bool:
    """Ask the durable ledger; default to *notify* if it doesn't exist yet."""
    from temporalio.service import RPCError

    from ynab_agent.workflow.alert_types import (
        ALERT_LEDGER_WORKFLOW_ID,
        ShouldNotifyRequest,
    )
    from ynab_agent.workflow.temporal_client import client

    temporal = await client()
    handle = temporal.get_workflow_handle(ALERT_LEDGER_WORKFLOW_ID)
    try:
        decision: bool = await handle.query(
            "should_notify",
            ShouldNotifyRequest(key=key, now=now),
            result_type=bool,
        )
    except RPCError:
        # No ledger started yet → nothing has ever alerted → notify.
        return True
    return decision


def _build_notification(alert: FailureAlert) -> Notification:
    from ynab_agent.notify.client import Notification

    return Notification(
        title=alert.title,
        body=alert.body,
        priority="high",
        tags=("rotating_light",),
    )


def _client_or_none() -> NotifyClient | None:
    from ynab_agent.notify.client import NotifyClient

    try:
        return NotifyClient.from_env()
    except RuntimeError:
        return None


async def _record(key: str) -> None:
    """Signal-with-start the ledger so this key's cooldown begins now."""
    from temporalio.common import WorkflowIDConflictPolicy

    from ynab_agent.workflow.alert_types import (
        ALERT_LEDGER_WORKFLOW_ID,
        LedgerParams,
    )
    from ynab_agent.workflow.temporal_client import client, task_queue

    temporal = await client()
    await temporal.start_workflow(
        "AlertLedgerWorkflow",
        LedgerParams(),
        id=ALERT_LEDGER_WORKFLOW_ID,
        task_queue=task_queue(),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="record",
        start_signal_args=[key],
    )
