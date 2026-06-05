"""Compose an operator alert from a terminal activity failure (SPEC §13).

A pure helper used by a workflow's failure hook: it reads only replay-safe
fields off the ``ActivityError`` (the activity name and the underlying cause)
and turns them into the :class:`FailureAlert` the ``alert_failure`` activity
pushes. Kept free of Temporal commands so the hook stays deterministic; the SDK
exception types it inspects are sandbox-safe.
"""

from __future__ import annotations

from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

from ynab_agent.workflow.alert_types import FailureAlert


def build_failure_alert(
    *, key: str, context: str, exc: ActivityError
) -> FailureAlert:
    """The operator alert for a terminal activity failure. Pure.

    ``key`` drives dedup (the transaction id); ``context`` is a human locator
    (payee + txn id) for the push body.
    """
    activity_name = exc.activity_type or "an activity"
    body = f"{context}\n{activity_name} failed after retries — {_explain(exc)}"
    return FailureAlert(
        key=key,
        title=f"ynab-agent: {activity_name} failed",
        body=body,
    )


def _explain(exc: ActivityError) -> str:
    """A short, human reason from the error's cause (bug detail or timeout)."""
    cause = exc.__cause__
    if isinstance(cause, ApplicationError):
        return f"{cause.type or 'error'}: {cause}"
    if isinstance(cause, TemporalTimeoutError):
        kind = cause.type.name if cause.type is not None else "timeout"
        return f"timed out ({kind})"
    return str(cause) if cause is not None else "unknown failure"
