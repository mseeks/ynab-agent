"""``assemble`` composes the view and turns errors into source dots."""

from __future__ import annotations

from datetime import UTC, datetime

from ynab_agent.dashboard import read_model
from ynab_agent.dashboard.model import (
    Budget,
    Conversation,
    DashboardModel,
    Deploy,
    OfferRow,
    QueueItem,
    RunTelemetry,
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
) -> DashboardModel:
    return read_model.assemble(
        now=_NOW,
        repo="mseeks/ynab-agent",
        temporal=temporal,
        ynab=ynab,
        clickhouse=clickhouse,
        agentmail=agentmail,
        github=github,
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
    kinds = sorted(q.kind for q in model.queue)
    assert kinds == ["offer", "proposal"]
    offer = next(q for q in model.queue if q.kind == "offer")
    assert offer.label == "Spotify"


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
    assert model.heartbeat.poll_live is True
    assert model.autonomy.eligible == 1
    assert model.autonomy.blessed == 2
    assert model.lifecycle.archived == 9
