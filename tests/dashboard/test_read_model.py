"""``assemble`` composes the view and turns errors into source dots."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ynab_agent.dashboard import read_model
from ynab_agent.dashboard.model import (
    ActivityStat,
    Budget,
    Conversation,
    DashboardModel,
    Deploy,
    Failure,
    OfferRow,
    QueueItem,
    RuleRow,
    RunTelemetry,
    StateCount,
    TxnFacts,
)
from ynab_agent.dashboard.temporal_source import TemporalReadout

_NOW = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)
_OFF_TEMPORAL: tuple[TemporalReadout, str | None] = (TemporalReadout(), None)
_OFF_YNAB: tuple[Budget, str | None] = (Budget(available=False), "off")
_OFF_CH: tuple[RunTelemetry, str | None] = (
    RunTelemetry(available=False),
    "off",
)
_OFF_MAIL: tuple[tuple[Conversation, ...], str | None] = ((), "off")
_OFF_GH: tuple[Deploy, str | None] = (Deploy(), "off")


def _assemble(
    *,
    temporal: tuple[TemporalReadout, str | None] = _OFF_TEMPORAL,
    ynab: tuple[Budget, str | None] = _OFF_YNAB,
    clickhouse: tuple[RunTelemetry, str | None] = _OFF_CH,
    agentmail: tuple[tuple[Conversation, ...], str | None] = _OFF_MAIL,
    github: tuple[Deploy, str | None] = _OFF_GH,
    queue_facts: dict[str, TxnFacts] | None = None,
) -> DashboardModel:
    return read_model.assemble(
        now=_NOW,
        repo="mseeks/ynab-agent",
        temporal=temporal,
        ynab=ynab,
        clickhouse=clickhouse,
        agentmail=agentmail,
        github=github,
        queue_facts=queue_facts,
    )


def test_errors_become_red_source_dots() -> None:
    model = _assemble(temporal=(TemporalReadout(), "RPCError: down"))
    by_name = {s.name: s for s in model.sources}
    assert by_name["temporal"].ok is False
    assert by_name["temporal"].detail == "RPCError: down"
    # "off" is also not-ok (a muted/absent source), never silently green.
    assert by_name["ynab"].ok is False
    assert by_name["clickhouse"].ok is False


def test_healthy_sources_show_a_summary() -> None:
    model = _assemble(
        temporal=(TemporalReadout(in_flight=3), None),
        ynab=(Budget(available=True, unapproved=5), None),
        agentmail=(
            (Conversation(subject="s", preview="", kind="thread"),),
            None,
        ),
    )
    by_name = {s.name: s for s in model.sources}
    assert by_name["temporal"].ok is True
    assert by_name["temporal"].detail == "3 in-flight"
    assert by_name["ynab"].detail == "5 unapproved"
    assert by_name["agentmail"].detail == "1 threads"


def test_queue_merges_proposals_and_offers() -> None:
    readout = TemporalReadout(
        awaiting=(QueueItem(kind="proposal", label="t1", ident="t1"),),
        offers=(OfferRow(rule_id="r1", payee="Spotify", status="running"),),
    )
    model = _assemble(temporal=(readout, None))
    kinds = sorted(q.kind for q in model.needs_you)
    assert kinds == ["offer", "proposal"]
    offer = next(q for q in model.needs_you if q.kind == "offer")
    assert offer.label == "Auto-handle Spotify?"


def test_queue_splits_needs_you_from_already_handled() -> None:
    readout = TemporalReadout(
        awaiting=(
            QueueItem(kind="proposal", label="t1", ident="t1"),
            QueueItem(kind="proposal", label="t2", ident="t2"),
            QueueItem(kind="proposal", label="t3", ident="t3"),
        ),
    )
    facts = {
        "t1": TxnFacts(payee="Amazon", amount="-$5.00", approved=False),
        "t2": TxnFacts(payee="CP Energy", amount="-$61.00", approved=True),
        # t3 unresolved (YNAB couldn't find it) → defaults to needs-you.
    }
    model = _assemble(temporal=(readout, None), queue_facts=facts)
    assert {q.ident for q in model.needs_you} == {"t1", "t3"}
    assert {q.ident for q in model.handled} == {"t2"}  # approved → winding down
    row = next(q for q in model.needs_you if q.ident == "t1")
    assert row.payee == "Amazon"
    assert row.amount == "-$5.00"


def test_rule_categories_show_names_not_ids() -> None:
    readout = TemporalReadout(
        rules=(
            RuleRow(
                payee="Spotify",
                category="11111111-aaaa",
                trust="trusted",
                source="learned",
                hits=3,
                offered=False,
            ),
            RuleRow(
                payee="Costco",
                category="split",
                trust="confirmed",
                source="learned",
                hits=2,
                offered=False,
            ),
            RuleRow(
                payee="Mystery",
                category="99999999-bbbb",
                trust="confirmed",
                source="learned",
                hits=1,
                offered=False,
            ),
        ),
    )
    budget = Budget(available=True, categories={"11111111-aaaa": "Music"})
    model = _assemble(temporal=(readout, None), ynab=(budget, None))
    cats = {r.payee: r.category for r in model.autonomy.rules}
    assert cats["Spotify"] == "Music"  # id resolved to its YNAB name
    assert cats["Costco"] == "split"  # split target left as-is
    assert cats["Mystery"] == "99999999"  # unknown id → short stub, not a UUID


def test_proposal_borrows_its_thread_subject_as_the_label() -> None:
    readout = TemporalReadout(
        awaiting=(QueueItem(kind="proposal", label="t1", ident="t1"),),
    )
    convo = Conversation(
        subject="Amazon — -$5.00 · Shopping?",
        preview="",
        kind="proposal",
        ref="t1",
    )
    model = _assemble(temporal=(readout, None), agentmail=((convo,), None))
    assert model.needs_you[0].question == "Amazon — -$5.00 · Shopping?"
    assert model.needs_you[0].label == "Amazon — -$5.00 · Shopping?"


def test_temporal_pieces_slot_into_the_model() -> None:
    readout = TemporalReadout(
        poll_status="completed",
        poll_live=True,
        observe=4,
        eligible=1,
        blessed=2,
        archived=9,
    )
    model = _assemble(temporal=(readout, None))
    assert model.health.poll_live is True
    assert model.autonomy.eligible == 1
    assert model.autonomy.blessed == 2
    assert model.lifecycle.archived == 9


def test_narrative_reflects_the_numbers() -> None:
    awaiting = tuple(
        QueueItem(kind="proposal", label=f"t{i}", ident=f"t{i}")
        for i in range(1, 4)
    )
    readout = TemporalReadout(
        poll_live=True,
        in_flight=10,
        lifecycle_states=(
            StateCount(state="open", count=7),
            StateCount(state="awaiting_human", count=3),
        ),
        awaiting=awaiting,
    )
    # t1 unapproved (needs you), t2/t3 already settled in YNAB.
    facts = {
        "t1": TxnFacts(payee="p", amount="-$1.00", approved=False),
        "t2": TxnFacts(payee="p", amount="-$1.00", approved=True),
        "t3": TxnFacts(payee="p", amount="-$1.00", approved=True),
    }
    model = _assemble(
        temporal=(readout, None),
        ynab=(Budget(available=True, unapproved=0), None),
        queue_facts=facts,
    )
    assert "waiting on you" in model.narrative.headline
    body = model.narrative.paragraphs[0]
    assert "caught up in YNAB" in body
    assert "tracking 10 transactions" in body
    assert "2 you already settled in YNAB" in body


def test_health_verdict_branches() -> None:
    # Poll dead → the worst verdict.
    m = _assemble(temporal=(TemporalReadout(poll_live=False), None))
    assert (m.health.tone, m.health.label) == ("bad", "down")
    # Poll live but YNAB off (the default) → degraded.
    m = _assemble(temporal=(TemporalReadout(poll_live=True), None))
    assert (m.health.tone, m.health.label) == ("warn", "degraded")
    # Poll live + money source reachable → healthy; poll_status threads through.
    m = _assemble(
        temporal=(TemporalReadout(poll_live=True, poll_status="running"), None),
        ynab=(Budget(available=True, unapproved=0), None),
    )
    assert (m.health.tone, m.health.label) == ("ok", "healthy")
    assert m.health.poll_status == "running"


def test_a_stalled_poll_reads_down_not_healthy() -> None:
    # The server says RUNNING even with a dead worker — the run just never
    # advances. No fresh tick (continue-as-new start) in 3h means ingestion
    # has stopped, the exact condition the §13 deadman pages on.
    stale = TemporalReadout(
        poll_live=True,
        poll_status="running",
        poll_last_start=_NOW - timedelta(hours=4),
    )
    m = _assemble(temporal=(stale, None), ynab=(Budget(available=True), None))
    assert m.health.poll_stale is True
    assert (m.health.tone, m.health.label) == ("bad", "down")
    fresh = stale.model_copy(
        update={"poll_last_start": _NOW - timedelta(hours=1)}
    )
    m2 = _assemble(temporal=(fresh, None), ynab=(Budget(available=True), None))
    assert m2.health.poll_stale is False
    assert m2.health.tone == "ok"


def test_worker_staleness_spans_two_poll_intervals() -> None:
    # A quiet worker legitimately does nothing between hourly ticks: 90
    # minutes of span silence is normal (the old one-interval threshold
    # flapped degraded every hour); three hours is a real warning.
    live = TemporalReadout(
        poll_live=True, poll_status="running", poll_last_start=_NOW
    )
    quiet = RunTelemetry(
        available=True,
        total_spans=10,
        last_activity=_NOW - timedelta(minutes=90),
    )
    m = _assemble(
        temporal=(live, None),
        ynab=(Budget(available=True), None),
        clickhouse=(quiet, None),
    )
    assert m.health.tone == "ok"
    silent = quiet.model_copy(
        update={"last_activity": _NOW - timedelta(hours=3)}
    )
    m2 = _assemble(
        temporal=(live, None),
        ynab=(Budget(available=True), None),
        clickhouse=(silent, None),
    )
    assert m2.health.tone == "warn"


def test_off_sources_are_marked_off_not_broken() -> None:
    # Deliberately-unconfigured optional sources are a choice, not a fault —
    # they must be distinguishable from a configured-but-broken one.
    m = _assemble(temporal=(TemporalReadout(), "RPCError: down"))
    by_name = {s.name: s for s in m.sources}
    assert by_name["clickhouse"].off is True  # the "off" fixtures
    assert by_name["github"].off is True
    assert by_name["temporal"].off is False  # genuinely broken
    assert by_name["temporal"].ok is False


def test_offer_fallbacks_never_show_a_raw_uuid() -> None:
    with_payee = TemporalReadout(
        offers=(OfferRow(rule_id="r9", payee="Spotify", status="running"),),
    )
    m = _assemble(temporal=(with_payee, None))
    offer = next(q for q in m.needs_you if q.kind == "offer")
    assert offer.label == "Auto-handle Spotify?"

    bare = TemporalReadout(
        offers=(
            OfferRow(
                rule_id="0123456789abcdef0123", payee="", status="running"
            ),
        ),
    )
    m2 = _assemble(temporal=(bare, None))
    offer2 = next(q for q in m2.needs_you if q.kind == "offer")
    assert offer2.label == "autonomy offer 01234567"
    assert "0123456789abcdef0123" not in offer2.label


def test_health_warns_on_high_span_error_rate() -> None:
    tele = RunTelemetry(
        available=True, total_spans=1000, error_spans=200, last_activity=_NOW
    )
    m = _assemble(
        temporal=(TemporalReadout(poll_live=True), None),
        ynab=(Budget(available=True), None),
        clickhouse=(tele, None),
    )
    assert m.health.tone == "warn"  # 20% errored is over the 5% floor


def test_narrative_headline_branches() -> None:
    # Nothing pending → the calm headline.
    m = _assemble(
        temporal=(TemporalReadout(poll_live=True), None),
        ynab=(Budget(available=True, unapproved=0), None),
    )
    assert "caught up" in m.narrative.headline.lower()
    assert m.narrative.tone == "ok"
    # A bad verdict overrides everything else.
    m = _assemble(temporal=(TemporalReadout(poll_live=False), None))
    assert m.narrative.headline == "Attention needed."
    assert m.narrative.tone == "bad"


def test_narrative_includes_offers_and_failures() -> None:
    readout = TemporalReadout(
        poll_live=True,
        offers=(OfferRow(rule_id="r1", payee="Spotify", status="running"),),
        failures=(
            Failure(
                workflow_id="x", kind="failed", reason="boom", intentional=False
            ),
        ),
    )
    m = _assemble(
        temporal=(readout, None),
        ynab=(Budget(available=True, unapproved=0), None),
    )
    body = m.narrative.paragraphs[0]
    assert "autonomy offer" in body
    assert "failed recently" in body
    assert any(q.kind == "offer" for q in m.needs_you)


def test_intentional_resets_stay_out_of_the_real_failure_count() -> None:
    readout = TemporalReadout(
        poll_live=True,
        failures=(
            Failure(
                workflow_id="a",
                kind="failed",
                reason="timed out",
                intentional=False,
            ),
            Failure(
                workflow_id="b",
                kind="terminated",
                reason="go-live reset",
                intentional=True,
            ),
        ),
    )
    m = _assemble(
        temporal=(readout, None),
        ynab=(Budget(available=True, unapproved=0), None),
    )
    assert m.health.real_failures == 1  # the reset doesn't count


def test_offer_borrows_thread_subject() -> None:
    readout = TemporalReadout(
        offers=(OfferRow(rule_id="r9", payee="Spotify", status="running"),),
    )
    convo = Conversation(
        subject="Trust Spotify → Subscriptions?",
        preview="",
        kind="offer",
        ref="r9",
    )
    m = _assemble(temporal=(readout, None), agentmail=((convo,), None))
    offer = next(q for q in m.needs_you if q.kind == "offer")
    assert offer.question == "Trust Spotify → Subscriptions?"
    assert offer.label == "Trust Spotify → Subscriptions?"


def test_applied_count_drives_the_learning_clause() -> None:
    tele = RunTelemetry(
        available=True,
        total_spans=10,
        last_activity=_NOW,
        window_days=3,
        activities=(
            ActivityStat(name="commit_to_ynab", count=7, avg_ms=1, max_ms=2),
        ),
    )
    m = _assemble(
        temporal=(TemporalReadout(poll_live=True, observe=4), None),
        ynab=(Budget(available=True, unapproved=0), None),
        clickhouse=(tele, None),
    )
    body = m.narrative.paragraphs[0]
    assert "applied 7 categories in the last 3 days" in body
    assert "4 rules observed" in body
