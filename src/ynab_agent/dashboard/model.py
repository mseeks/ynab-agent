"""The dashboard view-model — frozen values rendered to one HTML page.

A pure projection assembled by :mod:`ynab_agent.dashboard.read_model` from the
source readers and rendered by :mod:`ynab_agent.dashboard.render`. Nothing here
performs I/O; every field is a plain value so the page is fully testable without
a cluster.
"""

from __future__ import annotations

from datetime import datetime

from ynab_agent.domain.base import Frozen


class SourceHealth(Frozen):
    """One data source's liveness dot (green when ``ok``, else red)."""

    name: str
    ok: bool
    detail: str


# ── Is it alive? (heartbeat) ─────────────────────────────────────────────────
class Heartbeat(Frozen):
    """The poll loop's pulse plus the worker/webhook last-seen watermarks."""

    poll_status: str
    poll_live: bool
    poll_last_start: datetime | None = None
    worker_last_span: datetime | None = None
    webhook_last_span: datetime | None = None


# ── Transaction lifecycle ────────────────────────────────────────────────────
class StateCount(Frozen):
    """A lifecycle state and how many in-flight transactions are in it."""

    state: str
    count: int


class Lifecycle(Frozen):
    """The in-flight W2 funnel plus recent terminal counts."""

    states: tuple[StateCount, ...] = ()
    in_flight: int = 0
    archived: int = 0
    terminated: int = 0


# ── Autonomy ladder (the rule registry) ──────────────────────────────────────
class RuleRow(Frozen):
    """One learned/blessed rule, reduced to what the ladder shows."""

    payee: str
    category: str
    trust: str
    source: str
    hits: int
    offered: bool
    last_confirmed_at: datetime | None = None


class OfferRow(Frozen):
    """One live autonomy-offer workflow (awaiting the owner's yes/no)."""

    rule_id: str
    payee: str
    status: str
    started_at: datetime | None = None


class Autonomy(Frozen):
    """The earned-autonomy picture: counts, the rule table, live offers."""

    observe: int = 0
    eligible: int = 0
    blessed: int = 0
    rules: tuple[RuleRow, ...] = ()
    offers: tuple[OfferRow, ...] = ()


# ── Awaiting a human ─────────────────────────────────────────────────────────
class QueueItem(Frozen):
    """Something waiting on the owner: an open proposal or a pending offer."""

    kind: str  # "proposal" | "offer"
    label: str
    ident: str
    since: datetime | None = None


# ── Budget (YNAB) ────────────────────────────────────────────────────────────
class CategoryRow(Frozen):
    """A category's month-to-date balance (negative = overspent)."""

    name: str
    balance: str
    overspent: bool


class TxnRow(Frozen):
    """A transaction reduced to a payee + amount display."""

    payee: str
    amount: str


class Budget(Frozen):
    """The budget surface: the unapproved backlog and overspent categories."""

    available: bool = False
    unapproved: int = 0
    unapproved_sample: tuple[TxnRow, ...] = ()
    overspent: tuple[CategoryRow, ...] = ()


# ── Conversations (AgentMail) ────────────────────────────────────────────────
class Conversation(Frozen):
    """A recent AgentMail thread reduced to a one-line summary."""

    subject: str
    preview: str
    kind: str  # "proposal" | "offer" | "thread"
    updated_at: datetime | None = None


# ── Inbound (W3 dispatch) ────────────────────────────────────────────────────
class DispatchTally(Frozen):
    """Recent inbound dispatch results, counted by routing action."""

    transaction: int = 0
    offer: int = 0
    receipt: int = 0
    command: int = 0
    quarantine: int = 0
    ignore: int = 0
    total: int = 0


# ── Run telemetry (ClickHouse) ───────────────────────────────────────────────
class ActivityStat(Frozen):
    """Per-activity run count and latency (from the trace spans)."""

    name: str
    count: int
    avg_ms: float
    max_ms: float


class RunTelemetry(Frozen):
    """Trace-derived health for the worker (best-effort enrichment)."""

    available: bool = False
    total_spans: int = 0
    error_spans: int = 0
    last_activity: datetime | None = None
    window_days: int = 3
    activities: tuple[ActivityStat, ...] = ()
    recent_errors: tuple[str, ...] = ()


# ── Failures ─────────────────────────────────────────────────────────────────
class Failure(Frozen):
    """A workflow that ended terminated/failed, with its recovered reason."""

    workflow_id: str
    kind: str
    reason: str | None = None
    when: datetime | None = None


# ── Deploy (GitHub) ──────────────────────────────────────────────────────────
class PrRow(Frozen):
    """A recent pull request reduced to title + state + CI."""

    number: int
    title: str
    state: str
    ci: str | None
    url: str
    when: datetime | None = None


class Deploy(Frozen):
    """Recent repo activity: PRs and their CI."""

    prs: tuple[PrRow, ...] = ()


# ── The whole page ───────────────────────────────────────────────────────────
class DashboardModel(Frozen):
    """Everything one dashboard request renders."""

    generated_at: datetime
    repo: str
    sources: tuple[SourceHealth, ...]
    heartbeat: Heartbeat
    lifecycle: Lifecycle
    autonomy: Autonomy
    queue: tuple[QueueItem, ...]
    budget: Budget
    conversations: tuple[Conversation, ...]
    dispatch: DispatchTally
    telemetry: RunTelemetry
    failures: tuple[Failure, ...]
    deploy: Deploy
