"""The dashboard renders a full, escaped page from any model (SPEC §15)."""

from __future__ import annotations

from datetime import UTC, datetime

from ynab_agent.dashboard import render
from ynab_agent.dashboard.model import (
    ActivityStat,
    Autonomy,
    Budget,
    CategoryRow,
    Conversation,
    DashboardModel,
    Deploy,
    DispatchTally,
    Heartbeat,
    Lifecycle,
    PrRow,
    QueueItem,
    RuleRow,
    RunTelemetry,
    SourceHealth,
    StateCount,
)

_NOW = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)


def _full() -> DashboardModel:
    return DashboardModel(
        generated_at=_NOW,
        repo="mseeks/ynab-agent",
        sources=(
            SourceHealth(name="temporal", ok=True, detail="3 in-flight"),
            SourceHealth(name="ynab", ok=False, detail="off"),
        ),
        heartbeat=Heartbeat(
            poll_status="completed",
            poll_live=True,
            poll_last_start=datetime(2026, 6, 5, 11, 0, tzinfo=UTC),
            worker_last_span=datetime(2026, 6, 5, 11, 59, tzinfo=UTC),
        ),
        lifecycle=Lifecycle(
            states=(
                StateCount(state="enriching", count=2),
                StateCount(state="awaiting_human", count=1),
            ),
            in_flight=3,
            archived=10,
            terminated=1,
        ),
        autonomy=Autonomy(
            observe=4,
            eligible=1,
            blessed=2,
            rules=(
                RuleRow(
                    payee="Spotify",
                    category="subscriptions",
                    trust="trusted",
                    source="human_explicit",
                    hits=6,
                    offered=True,
                ),
            ),
        ),
        queue=(QueueItem(kind="proposal", label="t1", ident="t1"),),
        budget=Budget(
            available=True,
            unapproved=5,
            overspent=(
                CategoryRow(name="Dining", balance="-$12.00", overspent=True),
            ),
        ),
        conversations=(
            Conversation(subject="Spotify", preview="ok", kind="proposal"),
        ),
        dispatch=DispatchTally(transaction=3, offer=1, total=5),
        telemetry=RunTelemetry(
            available=True,
            total_spans=120,
            error_spans=1,
            activities=(
                ActivityStat(name="enrich", count=10, avg_ms=900, max_ms=4000),
            ),
        ),
        failures=(),
        deploy=Deploy(
            prs=(
                PrRow(
                    number=10,
                    title="autonomy",
                    state="merged",
                    ci="passed",
                    url="https://x",
                ),
            )
        ),
    )


def test_full_model_renders_every_panel() -> None:
    html = render.page(_full())
    assert html.startswith("<!doctype html>")
    for marker in (
        "Is it alive?",
        "Transaction lifecycle",
        "Autonomy ladder",
        "Awaiting a human",
        "Budget",
        "Conversations",
        "Inbound",
        "Run telemetry",
        "Deploy",
        "Spotify",
        "Safety envelope",
    ):
        assert marker in html, marker


def test_html_is_escaped_at_the_boundary() -> None:
    model = _full().model_copy(
        update={
            "conversations": (
                Conversation(
                    subject="<script>x</script>", preview="", kind="thread"
                ),
            )
        }
    )
    html = render.page(model)
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_empty_model_still_renders() -> None:
    model = DashboardModel(
        generated_at=_NOW,
        repo="mseeks/ynab-agent",
        sources=(SourceHealth(name="temporal", ok=False, detail="down"),),
        heartbeat=Heartbeat(poll_status="none", poll_live=False),
        lifecycle=Lifecycle(),
        autonomy=Autonomy(),
        queue=(),
        budget=Budget(available=False),
        conversations=(),
        dispatch=DispatchTally(),
        telemetry=RunTelemetry(available=False),
        failures=(),
        deploy=Deploy(),
    )
    html = render.page(model)
    assert "No transactions in flight" in html
    assert "Unavailable" in html  # budget + telemetry both degrade cleanly
