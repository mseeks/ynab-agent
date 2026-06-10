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
    Failure,
    Health,
    Lifecycle,
    Narrative,
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
        narrative=Narrative(
            headline="1 thing waiting on you.",
            paragraphs=("You're nearly caught up in YNAB (5 unapproved).",),
            tone="ok",
        ),
        health=Health(
            tone="ok",
            label="healthy",
            poll_live=True,
            poll_status="running",
            poll_last_start=datetime(2026, 6, 5, 11, 0, tzinfo=UTC),
            worker_last_span=datetime(2026, 6, 5, 11, 59, tzinfo=UTC),
            span_error_rate=0.008,
            needs_you=1,
            real_failures=1,
        ),
        sources=(
            SourceHealth(name="temporal", ok=True, detail="3 in-flight"),
            SourceHealth(name="ynab", ok=False, detail="off"),
        ),
        needs_you=(
            QueueItem(
                kind="proposal",
                label="Amazon $-5.00",
                ident="t1",
                payee="Amazon",
                amount="$-5.00",
                category="Shopping",
                approved=False,
                question="Amazon — $-5.00 · Shopping?",
                since=datetime(2026, 6, 3, 12, 0, tzinfo=UTC),
            ),
        ),
        handled=(
            QueueItem(kind="proposal", label="t9", ident="t9", approved=True),
        ),
        lifecycle=Lifecycle(
            states=(
                StateCount(state="enriching", count=2),
                StateCount(state="awaiting_human", count=1),
                StateCount(state="open", count=7),
            ),
            in_flight=10,
            archived=0,
            terminated=4,
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
        failures=(
            Failure(
                workflow_id="abc",
                kind="failed",
                reason="Activity task timed out",
                when=_NOW,
                intentional=False,
            ),
            Failure(
                workflow_id="ynab-poll",
                kind="terminated",
                reason="go-live reset",
                when=_NOW,
                intentional=True,
            ),
        ),
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


def test_full_model_renders_every_zone() -> None:
    html = render.page(_full())
    assert html.startswith("<!doctype html>")
    for marker in (
        "ynab-agent",
        "1 thing waiting on you.",  # the narrative headline
        "Needs you",
        "Amazon — $-5.00 · Shopping?",  # humanized queue row
        "winding down",  # the handled footnote
        "Is it working?",
        "What it's done",
        "Budget",
        "Autonomy ladder",
        "Run telemetry",
        "Failures &amp; resets",
        "go-live reset",  # the intentional reset, kept under disclosure
        "Deploy",
        "Safety envelope",
    ):
        assert marker in html, marker


def test_masthead_stamp_is_in_household_time() -> None:
    # The "as of" time shows the household timezone (SPEC §13): 12:00 UTC on
    # 2026-06-05 is 07:00 CDT in US Central, not UTC.
    html = render.page(_full())
    assert "2026-06-05 07:00 CDT" in html
    assert "12:00 UTC" not in html


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
        narrative=Narrative(headline="All caught up — nothing needs you."),
        health=Health(tone="bad", label="down", poll_live=False),
        sources=(SourceHealth(name="temporal", ok=False, detail="down"),),
        needs_you=(),
        handled=(),
        lifecycle=Lifecycle(),
        autonomy=Autonomy(),
        budget=Budget(available=False),
        conversations=(),
        dispatch=DispatchTally(),
        telemetry=RunTelemetry(available=False),
        failures=(),
        deploy=Deploy(),
    )
    html = render.page(model)
    assert "All caught up" in html  # the empty-queue state
    assert "No transactions in flight" in html  # the empty flow
    assert "not configured" in html  # telemetry rail degrades cleanly


def test_needs_you_split_and_empty_states_render() -> None:
    full = _full()
    html = render.page(full)
    # The humanized question is the row title; the handled count is a footnote.
    assert "Amazon — $-5.00 · Shopping?" in html
    assert "1 more proposal" in html
    # An emptied queue shows the caught-up state and renders no queue rows.
    empty = full.model_copy(update={"needs_you": (), "handled": ()})
    html2 = render.page(empty)
    assert "all caught up" in html2.lower()
    assert 'class="qrow"' not in html2
