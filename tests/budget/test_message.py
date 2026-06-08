"""The overspend alert's deterministic wording and dedup label (W6, SPEC §7)."""

from __future__ import annotations

from ynab_agent.budget.message import (
    overspend_body,
    overspend_subject,
    overspend_thread_label,
    overspend_update_label,
)
from ynab_agent.budget.overspend import OverspendAssessment, OverspendVerdict
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money


def _assessment(
    verdict: OverspendVerdict = OverspendVerdict.TRENDING_OVER,
    *,
    spent: str = "250",
    projected: str = "500",
) -> OverspendAssessment:
    return OverspendAssessment(
        category=CategoryId("dining"),
        name="Dining",
        verdict=verdict,
        budgeted=Money.from_currency("400"),
        spent=Money.from_currency(spent),
        projected=Money.from_currency(projected),
    )


def test_subject_names_category_and_trend() -> None:
    assert overspend_subject(_assessment()) == "Dining: trending over budget"
    over = _assessment(OverspendVerdict.ALREADY_OVER)
    assert overspend_subject(over) == "Dining: already over budget"


def test_body_trending_states_figures_days_and_projection() -> None:
    body = overspend_body(_assessment(), 6)
    assert "trending over budget" in body
    assert "$250.00 spent of $400.00" in body
    assert "6 days left" in body
    assert "projected ~$500.00 by month-end" in body


def test_body_already_over_uses_trending_to_phrasing() -> None:
    over = _assessment(
        OverspendVerdict.ALREADY_OVER, spent="420", projected="520"
    )
    body = overspend_body(over, 6)
    assert "already over budget" in body
    assert "$420.00 spent of $400.00" in body
    assert "trending to ~$520.00 by month-end" in body


def test_body_days_left_pluralization_and_clamp() -> None:
    assert "1 day left" in overspend_body(_assessment(), 1)
    assert "0 days left" in overspend_body(_assessment(), 0)
    # A run on the last day can read negative; never show "-1 days".
    assert "0 days left" in overspend_body(_assessment(), -1)


def test_thread_label_is_one_per_category_period() -> None:
    # Stable across verdict and projected, so every alert and re-alert shares
    # the one thread, independent of how bad it got this period.
    base = overspend_thread_label(_assessment(projected="500"), "2026-06")
    worse = overspend_thread_label(_assessment(projected="560"), "2026-06")
    escalated = overspend_thread_label(
        _assessment(OverspendVerdict.ALREADY_OVER), "2026-06"
    )
    assert base == worse == escalated
    # ...but a new period is a new conversation.
    assert base != overspend_thread_label(_assessment(), "2026-07")


def test_update_label_is_stable_for_identical_alert() -> None:
    a = _assessment()
    assert overspend_update_label(a, "2026-06") == overspend_update_label(
        a, "2026-06"
    )


def test_update_label_changes_on_worsening_escalation_and_period() -> None:
    base = overspend_update_label(_assessment(projected="500"), "2026-06")
    worse = overspend_update_label(_assessment(projected="560"), "2026-06")
    escalated = overspend_update_label(
        _assessment(OverspendVerdict.ALREADY_OVER), "2026-06"
    )
    next_period = overspend_update_label(_assessment(), "2026-07")
    assert base != worse
    assert base != escalated
    assert base != next_period


def test_thread_and_update_labels_are_distinct() -> None:
    # Both ride on the same opening message; distinct namespaces keep the
    # per-alert send dedup independent of the thread lookup.
    a = _assessment()
    assert overspend_thread_label(a, "2026-06") != overspend_update_label(
        a, "2026-06"
    )
