"""The overspend alert's wording and dedup labels — pure, no model (SPEC §7).

The W6 alert is deterministic: the figures and the verdict are already decided
by the pure projection (:mod:`ynab_agent.budget.overspend`), so the email is
plain templating, not a model call. Kept apart from the activity glue so the
subject, body, and labels are unit-testable without Temporal or AgentMail.

Two labels, two jobs (:meth:`MailClient.alert_on_thread`). The *thread* label is
stable for a category-period, so every alert and re-alert lands on one email
thread (with the W7 offer on it): the overspend is one conversation. The
*update* label folds in the verdict and projected month-end, so it is the
per-alert send-dedup key. A *retry* of the same alert collapses (no duplicate);
a *worsening* re-alert (the one ``should_alert`` lets through mid-period)
carries a new update label and posts a fresh update on that thread.
"""

from __future__ import annotations

from ynab_agent.budget.overspend import OverspendAssessment, OverspendVerdict


def _status_phrase(verdict: OverspendVerdict) -> str:
    """The human verb for the verdict (``OK`` never reaches an alert)."""
    if verdict is OverspendVerdict.ALREADY_OVER:
        return "already over budget"
    return "trending over budget"


def _days_phrase(days_left: int) -> str:
    """``"6 days"`` / ``"1 day"`` — the time left in the month, not negative."""
    days = max(days_left, 0)
    unit = "day" if days == 1 else "days"
    return f"{days} {unit}"


def overspend_subject(assessment: OverspendAssessment) -> str:
    """The alert thread's subject — category + how it is tracking."""
    return f"{assessment.name}: {_status_phrase(assessment.verdict)}"


def overspend_body(assessment: OverspendAssessment, days_left: int) -> str:
    """The alert body: spend against budget, time left, month-end projection.

    e.g. ``Dining is trending over budget: $250.00 spent of $400.00, 6 days
    left, projected ~$500.00 by month-end.`` Money renders via ``Money``'s
    ``__str__``; ``spent``/``budgeted``/``projected`` are positive magnitudes.
    """
    trailer = (
        "trending to"
        if assessment.verdict is OverspendVerdict.ALREADY_OVER
        else "projected"
    )
    return (
        f"{assessment.name} is {_status_phrase(assessment.verdict)}: "
        f"{assessment.spent} spent of {assessment.budgeted}, "
        f"{_days_phrase(days_left)} left, "
        f"{trailer} ~{assessment.projected} by month-end."
    )


def overspend_thread_label(assessment: OverspendAssessment, period: str) -> str:
    """The category's alert-thread key for the period (SPEC §7).

    Stable across the period — independent of verdict and projected — so every
    alert and re-alert for a category lands on the *same* email thread (and the
    W7 balancer's offer on it): one conversation per overspend. The per-alert
    send dedup lives in :func:`overspend_update_label`, not here.
    """
    return f"yaspend-{assessment.category}-{period}"


def overspend_update_label(assessment: OverspendAssessment, period: str) -> str:
    """The per-alert send-dedup label (SPEC §7).

    Keyed on the verdict and projected the alert fired at, so a retry of the
    same alert collapses (no duplicate) while a materially-worse re-alert (the
    only kind ``should_alert`` admits mid-period) carries a new label and so
    posts a fresh update on the thread.
    """
    return (
        f"yaspend-update-{assessment.category}-{period}"
        f"-{assessment.verdict.value}-{assessment.projected.milliunits}"
    )
