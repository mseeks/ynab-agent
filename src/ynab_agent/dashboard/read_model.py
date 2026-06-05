"""Assemble the source readouts into one view-model (pure).

Each reader hands in ``(data, error)``; this composes the
:class:`~ynab_agent.dashboard.model.DashboardModel`, turns each error into a
source-health dot, and slots the Temporal-derived pieces into place. No I/O —
fully unit-testable from in-memory readouts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ynab_agent.dashboard.model import (
    Autonomy,
    DashboardModel,
    Heartbeat,
    Lifecycle,
    QueueItem,
    SourceHealth,
)

if TYPE_CHECKING:
    from datetime import datetime

    from ynab_agent.dashboard.model import (
        Budget,
        Conversation,
        Deploy,
        RunTelemetry,
    )
    from ynab_agent.dashboard.temporal_source import TemporalReadout


def _health(name: str, error: str | None, ok_detail: str) -> SourceHealth:
    """A source dot: green with a summary, or red with the error/'off'."""
    if error is None:
        return SourceHealth(name=name, ok=True, detail=ok_detail)
    return SourceHealth(name=name, ok=False, detail=error)


def assemble(
    *,
    now: datetime,
    repo: str,
    temporal: tuple[TemporalReadout, str | None],
    ynab: tuple[Budget, str | None],
    clickhouse: tuple[RunTelemetry, str | None],
    agentmail: tuple[tuple[Conversation, ...], str | None],
    github: tuple[Deploy, str | None],
) -> DashboardModel:
    """Compose the whole dashboard view from the source readouts."""
    t, t_err = temporal
    budget, ynab_err = ynab
    telemetry, ch_err = clickhouse
    conversations, mail_err = agentmail
    deploy, gh_err = github

    sources = (
        _health("temporal", t_err, f"{t.in_flight} in-flight"),
        _health("ynab", ynab_err, f"{budget.unapproved} unapproved"),
        _health("clickhouse", ch_err, f"{telemetry.total_spans} spans"),
        _health("agentmail", mail_err, f"{len(conversations)} threads"),
        _health("github", gh_err, f"{len(deploy.prs)} PRs"),
    )

    heartbeat = Heartbeat(
        poll_status=t.poll_status,
        poll_live=t.poll_live,
        poll_last_start=t.poll_last_start,
        worker_last_span=telemetry.last_activity,
    )
    lifecycle = Lifecycle(
        states=t.lifecycle_states,
        in_flight=t.in_flight,
        archived=t.archived,
        terminated=t.terminated,
    )
    autonomy = Autonomy(
        observe=t.observe,
        eligible=t.eligible,
        blessed=t.blessed,
        rules=t.rules,
        offers=t.offers,
    )
    offer_queue = tuple(
        QueueItem(
            kind="offer",
            label=offer.payee or offer.rule_id,
            ident=offer.rule_id,
            since=offer.started_at,
        )
        for offer in t.offers
    )
    queue = (*t.awaiting, *offer_queue)

    return DashboardModel(
        generated_at=now,
        repo=repo,
        sources=sources,
        heartbeat=heartbeat,
        lifecycle=lifecycle,
        autonomy=autonomy,
        queue=queue,
        budget=budget,
        conversations=conversations,
        dispatch=t.dispatch,
        telemetry=telemetry,
        failures=t.failures,
        deploy=deploy,
    )
