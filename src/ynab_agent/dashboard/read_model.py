"""Assemble the source readouts into one view-model (pure).

Each reader hands in ``(data, error)``; this composes the
:class:`~ynab_agent.dashboard.model.DashboardModel`, turning each error into a
source-health dot and each Temporal/YNAB/AgentMail piece into the operator's
shape: a composite :class:`~ynab_agent.dashboard.model.Health` verdict, a
humanized owner queue (split into *needs you* vs *already settled*), and a
deterministic plain-English :class:`~ynab_agent.dashboard.model.Narrative`. No
I/O — fully unit-testable from in-memory readouts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ynab_agent.dashboard.model import (
    Autonomy,
    DashboardModel,
    Health,
    Lifecycle,
    Narrative,
    QueueItem,
    SourceHealth,
)

if TYPE_CHECKING:
    from datetime import datetime

    from ynab_agent.dashboard.model import (
        Budget,
        Conversation,
        Deploy,
        Failure,
        RunTelemetry,
        TxnFacts,
    )
    from ynab_agent.dashboard.temporal_source import TemporalReadout

# A worker span older than this reads as a stale heartbeat (warn). Two poll
# intervals, NOT one: a quiet worker legitimately does nothing between hourly
# ticks, so a one-interval threshold flapped healthy/degraded every hour.
STALE_WORKER_SECS = 7200.0
# The W1 loop continues-as-new each tick, so a RUNNING run whose start is
# older than this means the worker has stopped processing it — the server
# keeps saying RUNNING with a dead worker. Mirrors the deadman's horizon
# (``poll_activities.POLL_STALE_AFTER``).
_STALE_POLL_SECS = 3 * 3600.0
# A trace error rate above this contributes a warn to the health rollup.
_ERROR_RATE_WARN = 0.05
# The activities whose run-count means "a category was written to YNAB".
_APPLIED_ACTIVITY = "commit_to_ynab"


def _health(name: str, error: str | None, ok_detail: str) -> SourceHealth:
    """A source dot: green, gray for deliberately-off, red for broken.

    An unconfigured optional source is a choice, not a fault, and must not
    read like breakage.
    """
    if error is None:
        return SourceHealth(name=name, ok=True, detail=ok_detail)
    if error == "off":
        return SourceHealth(name=name, ok=False, detail="off", off=True)
    return SourceHealth(name=name, ok=False, detail=error)


def _s(n: int) -> str:
    """The plural suffix for a count (1 → '', else 's')."""
    return "" if n == 1 else "s"


def _short(ident: str) -> str:
    """A short, readable stand-in when an id can't be humanized."""
    return ident[:8] if len(ident) > 8 else ident


def _age_phrase(seconds: float) -> str:
    """A coarse, friendly age ('4 days', '3 hours', 'moments')."""
    if seconds < 90:
        return "moments"
    if seconds < 5400:
        n = int(seconds // 60)
        return f"{n} minute{_s(n)}"
    if seconds < 129600:
        n = int(seconds // 3600)
        return f"{n} hour{_s(n)}"
    n = int(seconds // 86400)
    return f"{n} day{_s(n)}"


def _oldest_age(items: tuple[QueueItem, ...], now: datetime) -> str | None:
    """The age of the longest-waiting item, as a phrase (None if empty)."""
    # Normalize each to aware BEFORE min() — comparing aware vs naive raises.
    sinces = [_aware(q.since, now) for q in items if q.since is not None]
    if not sinces:
        return None
    return _age_phrase((now - min(sinces)).total_seconds())


def _humanize_queue(
    readout: TemporalReadout,
    queue_facts: dict[str, TxnFacts],
    convo_by_ref: dict[str, Conversation],
) -> tuple[tuple[QueueItem, ...], tuple[QueueItem, ...]]:
    """Turn bare awaiting ids into readable rows; split needs-you vs settled.

    A proposal whose transaction is already ``approved`` in YNAB is one the
    owner settled in-app — it lands in *handled* (it lapses on its own); every
    other proposal, and every live autonomy offer, lands in *needs you*. The
    best label is the proposal's email subject, then ``payee · amount``, then a
    short id.
    """
    needs_you: list[QueueItem] = []
    handled: list[QueueItem] = []

    for item in readout.awaiting:
        facts = queue_facts.get(item.ident)
        convo = convo_by_ref.get(item.ident)
        question = convo.subject if convo is not None else None
        payee = facts.payee if facts is not None else None
        amount = facts.amount if facts is not None else None
        approved = facts.approved if facts is not None else None
        if question:
            label = question
        elif payee:
            label = f"{payee} {amount}".strip()
        else:
            label = _short(item.ident)
        row = QueueItem(
            kind="proposal",
            label=label,
            ident=item.ident,
            since=item.since,
            payee=payee,
            amount=amount,
            category=facts.category if facts is not None else None,
            approved=approved,
            question=question,
        )
        (handled if approved is True else needs_you).append(row)

    for offer in readout.offers:
        convo = convo_by_ref.get(offer.rule_id)
        # Best label first: the offer email's subject, then the rule's payee,
        # then a *short* id with a human prefix — never a raw 32-char UUID.
        label = (
            (convo.subject if convo else None)
            or (f"Auto-handle {offer.payee}?" if offer.payee else None)
            or f"autonomy offer {_short(offer.rule_id)}"
        )
        needs_you.append(
            QueueItem(
                kind="offer",
                label=label,
                ident=offer.rule_id,
                since=offer.started_at,
                payee=offer.payee or None,
                question=convo.subject if convo else None,
            )
        )

    return tuple(needs_you), tuple(handled)


def _rollup(
    *,
    now: datetime,
    poll_live: bool,
    poll_status: str,
    poll_last_start: datetime | None,
    temporal_error: str | None,
    ynab_error: str | None,
    telemetry: RunTelemetry,
    failures: tuple[Failure, ...],
    needs_you: int,
) -> Health:
    """Reduce the readouts to the single masthead health verdict."""
    worker_last = telemetry.last_activity
    rate = (
        telemetry.error_spans / telemetry.total_spans
        if telemetry.available and telemetry.total_spans > 0
        else None
    )
    real_failures = sum(1 for f in failures if not f.intentional)
    worker_stale = (
        worker_last is not None
        and (now - _aware(worker_last, now)).total_seconds() > STALE_WORKER_SECS
    )
    # The server reports RUNNING even when no worker is processing the loop —
    # the run just never advances. A fresh start each tick (continue-as-new)
    # is the real heartbeat, so an old start means ingestion has stopped.
    poll_stale = (
        poll_live
        and poll_last_start is not None
        and (now - _aware(poll_last_start, now)).total_seconds()
        > _STALE_POLL_SECS
    )

    # Tone is *current* operational health — can it do its job right now. An
    # isolated historical failure is surfaced (chip + narrative) but doesn't
    # flip the verdict; a stuck worker, an unreachable money source, or a high
    # live error rate does.
    if not poll_live or poll_stale or temporal_error is not None:
        tone, label = "bad", "down"
    elif (
        worker_stale
        or ynab_error is not None
        or (rate is not None and rate > _ERROR_RATE_WARN)
    ):
        tone, label = "warn", "degraded"
    else:
        tone, label = "ok", "healthy"

    return Health(
        tone=tone,
        label=label,
        poll_live=poll_live,
        poll_stale=poll_stale,
        poll_status=poll_status,
        poll_last_start=poll_last_start,
        worker_last_span=worker_last,
        span_error_rate=rate,
        needs_you=needs_you,
        real_failures=real_failures,
    )


def _aware(when: datetime, now: datetime) -> datetime:
    return when if when.tzinfo is not None else when.replace(tzinfo=now.tzinfo)


def _applied_count(telemetry: RunTelemetry) -> int:
    """Recent writes to YNAB, from the commit activity's run count."""
    for activity in telemetry.activities:
        if activity.name == _APPLIED_ACTIVITY:
            return activity.count
    return 0


def narrate(
    *,
    now: datetime,
    health: Health,
    lifecycle: Lifecycle,
    needs_you: tuple[QueueItem, ...],
    handled: tuple[QueueItem, ...],
    budget: Budget,
    autonomy: Autonomy,
    telemetry: RunTelemetry,
) -> Narrative:
    """Compose the deterministic 'state of things' summary from the numbers.

    Every clause is guarded, so the paragraph reads naturally whether or not a
    given source is on. This is the trustworthy baseline; an optional LLM polish
    may later rephrase it for warmth, always falling back to exactly this text.
    """
    states = {s.state: s.count for s in lifecycle.states}
    proposals = tuple(q for q in needs_you if q.kind == "proposal")
    offers = tuple(q for q in needs_you if q.kind == "offer")
    p, h, offers_n = len(proposals), len(handled), len(offers)
    real_failures = health.real_failures

    # Headline — the single most important thing.
    if health.tone == "bad":
        headline = "Attention needed."
    elif p or offers_n:
        waiting = p + offers_n
        headline = f"{waiting} thing{_s(waiting)} waiting on you."
    else:
        headline = "All caught up — nothing needs you."

    sentences: list[str] = []

    if budget.available:
        u = budget.unapproved
        if u == 0:
            sentences.append("You're caught up in YNAB.")
        elif u <= 5:
            sentences.append(
                f"You're nearly caught up in YNAB ({u} unapproved)."
            )
        else:
            sentences.append(f"{u} transactions await approval in YNAB.")

    in_flight = lifecycle.in_flight
    if in_flight:
        open_n = states.get("open", 0)
        awaiting_total = p + h
        other = max(0, in_flight - open_n - awaiting_total)
        parts = [f"{open_n} categorized and resting"] if open_n else []
        if h:
            parts.append(f"{h} you already settled in YNAB")
        if p:
            parts.append(f"{p} waiting on your reply")
        if other:
            parts.append(f"{other} still being processed")
        body = ", ".join(parts) if parts else "all winding down"
        sentences.append(
            f"The agent is tracking {in_flight} "
            f"transaction{_s(in_flight)}: {body}."
        )
    else:
        sentences.append("No transactions are in flight.")

    applied = _applied_count(telemetry)
    if telemetry.available and applied:
        obs = autonomy.observe
        tail = f" and is learning ({obs} rule{_s(obs)} observed)" if obs else ""
        sentences.append(
            f"It has applied {applied} categor{'y' if applied == 1 else 'ies'} "
            f"in the last {telemetry.window_days} days{tail}."
        )

    closing: list[str] = []
    if offers_n:
        verb = "s" if offers_n == 1 else ""
        closing.append(
            f"{offers_n} autonomy offer{_s(offers_n)} await{verb} your yes/no"
        )
    oldest = _oldest_age(proposals, now)
    if p and oldest:
        closing.append(f"the oldest reply has been waiting {oldest}")
    if h:
        closing.append("the settled ones lapse on their own")
    if real_failures:
        closing.append(
            f"{real_failures} workflow{_s(real_failures)} failed "
            "recently — check Failures"
        )
    if not p and not offers_n and not closing:
        closing.append("nothing needs you right now")
    if closing:
        joined = closing[0] + (
            "" if len(closing) == 1 else "; " + "; ".join(closing[1:])
        )
        sentences.append(joined[0].upper() + joined[1:] + ".")

    return Narrative(
        headline=headline, paragraphs=(" ".join(sentences),), tone=health.tone
    )


def assemble(
    *,
    now: datetime,
    repo: str,
    temporal: tuple[TemporalReadout, str | None],
    ynab: tuple[Budget, str | None],
    clickhouse: tuple[RunTelemetry, str | None],
    agentmail: tuple[tuple[Conversation, ...], str | None],
    github: tuple[Deploy, str | None],
    queue_facts: dict[str, TxnFacts] | None = None,
) -> DashboardModel:
    """Compose the whole dashboard view from the source readouts."""
    t, t_err = temporal
    budget, ynab_err = ynab
    telemetry, ch_err = clickhouse
    conversations, mail_err = agentmail
    deploy, gh_err = github
    facts = queue_facts or {}

    sources = (
        _health("temporal", t_err, f"{t.in_flight} in-flight"),
        _health("ynab", ynab_err, f"{budget.unapproved} unapproved"),
        _health("clickhouse", ch_err, f"{telemetry.total_spans} spans"),
        _health("agentmail", mail_err, f"{len(conversations)} threads"),
        _health("github", gh_err, f"{len(deploy.prs)} PRs"),
    )

    convo_by_ref = {c.ref: c for c in conversations if c.ref}
    needs_you, handled = _humanize_queue(t, facts, convo_by_ref)

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
    health = _rollup(
        now=now,
        poll_live=t.poll_live,
        poll_status=t.poll_status,
        poll_last_start=t.poll_last_start,
        temporal_error=t_err,
        ynab_error=ynab_err,
        telemetry=telemetry,
        failures=t.failures,
        needs_you=len(needs_you),
    )
    narrative = narrate(
        now=now,
        health=health,
        lifecycle=lifecycle,
        needs_you=needs_you,
        handled=handled,
        budget=budget,
        autonomy=autonomy,
        telemetry=telemetry,
    )

    return DashboardModel(
        generated_at=now,
        repo=repo,
        narrative=narrative,
        health=health,
        sources=sources,
        needs_you=needs_you,
        handled=handled,
        lifecycle=lifecycle,
        autonomy=autonomy,
        budget=budget,
        conversations=conversations,
        dispatch=t.dispatch,
        telemetry=telemetry,
        failures=t.failures,
        deploy=deploy,
    )
