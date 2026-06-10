"""The dashboard view-model — frozen values rendered to one HTML page.

A pure projection assembled by :mod:`ynab_agent.dashboard.read_model` from the
source readers and rendered by :mod:`ynab_agent.dashboard.render`. Nothing here
performs I/O; every field is a plain value so the page is fully testable without
a cluster.

The shape is organized around the *questions an operator actually asks* — "is it
healthy?", "do I need to do anything?", "what has it done?" — not around the
subsystems the data came from. :class:`Narrative` and :class:`Health` are the
headline projections that answer the first two at a glance.
"""

from __future__ import annotations

from datetime import datetime

from ynab_agent.domain.base import Frozen


class SourceHealth(Frozen):
    """One data source's liveness dot.

    Green when ``ok``; gray when ``off`` (deliberately unconfigured — not a
    fault); red otherwise (configured but broken).
    """

    name: str
    ok: bool
    detail: str
    off: bool = False


# ── State of things (the narrative) ──────────────────────────────────────────
class Narrative(Frozen):
    """The plain-English 'state of things' — deterministic, render-ready.

    A composed-from-the-numbers summary so a human comprehends the whole board
    without decoding panels. ``tone`` drives its accent; ``paragraphs`` is one
    or two friendly sentences. Built deterministically in
    :func:`~ynab_agent.dashboard.read_model.narrate`; an optional LLM polish may
    later rephrase the prose, always falling back to this text.
    """

    headline: str
    paragraphs: tuple[str, ...] = ()
    tone: str = "ok"  # "ok" | "warn" | "bad"


# ── Is it healthy? (the masthead rollup) ─────────────────────────────────────
class Health(Frozen):
    """The single composite health verdict shown in the masthead.

    ``tone``/``label`` are the one status dot + word; the rest are the few
    figures the masthead surfaces beside it. ``needs_you`` is *operation*, not
    *fault* — a healthy agent can still have items waiting on the owner.
    """

    tone: str = "ok"  # "ok" | "warn" | "bad"
    label: str = "healthy"
    poll_live: bool = False
    poll_stale: bool = False  # RUNNING on the server but no fresh tick
    poll_status: str = "none"  # the W1 poll's latest execution status
    poll_last_start: datetime | None = None
    worker_last_span: datetime | None = None
    span_error_rate: float | None = None
    needs_you: int = 0
    real_failures: int = 0


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


# ── The human's queue ────────────────────────────────────────────────────────
class TxnFacts(Frozen):
    """YNAB facts resolved for a queued transaction (the humanizing join).

    Keyed by ``ynab_id`` outside this type; produced by the YNAB source so the
    read-model can purely turn a bare workflow id into a readable row.
    ``approved`` is the pivotal signal: an awaiting proposal whose transaction
    is already ``approved`` in YNAB is one the owner settled *in-app* — it will
    lapse on its own and does not need them.
    """

    payee: str
    amount: str
    approved: bool
    category: str | None = None


class QueueItem(Frozen):
    """Something waiting on the owner: an open proposal or a pending offer.

    ``ident`` is the stable key (the YNAB txn id for a proposal, the rule id for
    an offer); the optional humanizing fields are filled by the join when the
    facts are reachable, and the renderer degrades to ``label`` (a short id)
    when they are not.
    """

    kind: str  # "proposal" | "offer"
    label: str
    ident: str
    since: datetime | None = None
    payee: str | None = None
    amount: str | None = None
    category: str | None = None
    approved: bool | None = None  # YNAB approved flag; None = unknown / offer
    question: str | None = None  # the proposal's email subject, when matched


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
    """A recent AgentMail thread reduced to a one-line summary.

    ``ref`` carries the transaction/rule id recovered from the agent's own
    ``yatxn-``/``yaoffer-`` label, so the queue can borrow a thread's subject as
    the most human description of the item it is waiting on.
    """

    subject: str
    preview: str
    kind: str  # "proposal" | "offer" | "thread"
    updated_at: datetime | None = None
    ref: str | None = None


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
    """A workflow that ended terminated/failed, with its recovered reason.

    ``intentional`` marks the operator's own go-live / re-test / reset
    terminations, so the page can keep them out of the fault headline (they are
    housekeeping, not breakage) while still listing them under disclosure.
    """

    workflow_id: str
    kind: str
    reason: str | None = None
    when: datetime | None = None
    intentional: bool = False


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
    """Everything one dashboard request renders.

    ``narrative`` and ``health`` lead (the at-a-glance answer); ``needs_you`` is
    the only acted-upon queue, ``handled`` the already-settled proposals still
    winding down. The remaining fields back the supporting zones and the
    progressive-disclosure rails.
    """

    generated_at: datetime
    repo: str
    narrative: Narrative
    health: Health
    sources: tuple[SourceHealth, ...]
    needs_you: tuple[QueueItem, ...]
    handled: tuple[QueueItem, ...]
    lifecycle: Lifecycle
    autonomy: Autonomy
    budget: Budget
    conversations: tuple[Conversation, ...]
    dispatch: DispatchTally
    telemetry: RunTelemetry
    failures: tuple[Failure, ...]
    deploy: Deploy
