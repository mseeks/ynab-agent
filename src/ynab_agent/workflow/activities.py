"""The I/O ports of the transaction lifecycle, as Temporal activities.

Every side effect the workflow performs — reading YNAB, committing a write,
sending email, the agentic enrichment/interpretation/converge steps — is an
activity, so neither the model's nor the spine's I/O runs in workflow code
(SPEC §0.5). These are *stubs*: typed signatures with no body. The real
implementations (YNAB/AgentMail MCP, Pydantic AI) are wired in a later step; the
workflow tests register mock implementations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from temporalio import activity

from ynab_agent.domain.allocations import ProposedCategory, ProposedSplit
from ynab_agent.domain.effects import FeedRuleLearning, MessagePurpose
from ynab_agent.domain.events import ConvergeOutcome, EnrichmentOutcome
from ynab_agent.domain.proposal import Decision, Proposal
from ynab_agent.domain.signals import InboundSignal
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.policy.converge import TargetState
from ynab_agent.workflow.types import ReplyOutcome

if TYPE_CHECKING:
    # Annotation-only: never imported at runtime, so the agentic/model stack
    # (pydantic-ai) never enters the workflow sandbox. The enrich body imports
    # it lazily, inside the activity, where it runs outside the sandbox.
    from ynab_agent.agentic.enrich import CandidateCategory
    from ynab_agent.domain.rule import Rule
    from ynab_agent.policy.floor import AutoActionCounters

_STUB = "workflow activity stub — register a real or mock implementation"


@activity.defn
async def fetch_snapshot(ynab_id: str) -> YnabSnapshot | None:
    """Read the current YNAB snapshot, or ``None`` if not yet imported.

    The YNAB client is imported lazily (keeping httpx out of the sandbox) and
    its blocking call runs off the event loop.
    """
    import asyncio

    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    return await asyncio.to_thread(client.snapshot, ynab_id)


async def _load_enrichment_inputs(
    snapshot: YnabSnapshot,
) -> tuple[tuple[CandidateCategory, ...], list[Rule], AutoActionCounters]:
    """Fetch the candidate categories, in-scope rules, and auto counters.

    The YNAB read and the rule-store read; stubbed until YNAB is wired.
    """
    raise NotImplementedError(_STUB)


@activity.defn
async def enrich(snapshot: YnabSnapshot) -> EnrichmentOutcome:
    """Assemble the proposal and route via the gate (the agentic middle).

    The model stack is imported lazily, here in the activity body, so it never
    enters the workflow sandbox. The gate decides autonomy from the rules; the
    agent runs only to produce the proposal a human is asked to confirm.
    """
    from datetime import UTC, datetime

    from ynab_agent.agentic.enrich import decide_enrichment

    candidates, rules, counters = await _load_enrichment_inputs(snapshot)
    return await decide_enrichment(
        snapshot, candidates, rules, counters, now=datetime.now(UTC)
    )


@activity.defn
async def commit_to_ynab(ynab_id: str, decision: Decision) -> None:
    """Commit a decision to YNAB (the deterministic write).

    Lazy YNAB client (httpx stays out of the sandbox); the blocking write runs
    off the event loop.
    """
    import asyncio

    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    await asyncio.to_thread(client.commit, ynab_id, decision)


@activity.defn
async def read_back(ynab_id: str) -> TargetState | None:
    """Read the post-write end-state for verification, or ``None`` if unread."""
    import asyncio

    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    return await asyncio.to_thread(client.read_back, ynab_id)


def _txn_label(ynab_id: str) -> str:
    """The per-transaction idempotency label (open-thread dedup; SPEC §5)."""
    return f"yatxn-{ynab_id}"


def _seq_label(ynab_id: str, action_seq: int) -> str:
    """The per-action idempotency label (send dedup; SPEC §3)."""
    return f"yaseq-{ynab_id}-{action_seq}"


def _subject(snapshot: YnabSnapshot) -> str:
    """The deterministic email subject (only the body is model-written)."""
    return f"[YNAB] {snapshot.payee} — {snapshot.amount}"


def _allocation_display(
    allocation: ProposedCategory | ProposedSplit, names: dict[str, str]
) -> str:
    """A human display of the proposed allocation, by category name."""
    if isinstance(allocation, ProposedCategory):
        return names.get(str(allocation.category), str(allocation.category))
    return " + ".join(
        names.get(str(line.category), str(line.category))
        for line in allocation.lines
    )


async def _read_for_compose(
    ynab_id: str,
) -> tuple[YnabSnapshot, dict[str, str]]:
    """Re-read the YNAB snapshot and a category-id→name map for composing.

    Both come from YNAB (the source of truth) at send time — there is no stored
    copy of the facts or the category names (SPEC §0.5, store-free).
    """
    import asyncio

    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    snapshot = await asyncio.to_thread(client.snapshot, ynab_id)
    if snapshot is None:
        msg = f"transaction {ynab_id} not found in YNAB at compose time"
        raise RuntimeError(msg)
    spends = await asyncio.to_thread(client.category_spends)
    names = {str(spend.category): spend.name for spend in spends}
    return snapshot, names


async def _compose_body(
    snapshot: YnabSnapshot,
    proposal: Proposal | None,
    purpose: MessagePurpose,
    names: dict[str, str],
) -> str:
    """Compose one message body for the thread (the agentic prose; SPEC §5).

    The model stack is imported lazily so pydantic-ai never enters the workflow
    sandbox. ``alternatives`` is left empty until enrichment carries candidate
    categories into the proposal (a later slice); the compose agent degrades
    gracefully, simply not listing alternatives when there are none.
    """
    from ynab_agent.agentic.compose import ComposeRequest, compose

    proposed = (
        _allocation_display(proposal.allocation, names)
        if proposal is not None
        else None
    )
    request = ComposeRequest(
        purpose=purpose.value,
        payee=snapshot.payee,
        amount_display=str(snapshot.amount),
        txn_date=snapshot.txn_date.isoformat(),
        memo=snapshot.memo,
        proposed_category=proposed,
        rationale=proposal.rationale if proposal is not None else None,
    )
    return await compose(request)


@activity.defn
async def open_thread(ynab_id: str, proposal: Proposal | None) -> str:
    """Open the AgentMail thread by sending the proposal; returns its id.

    A thread starts on its first send (AgentMail has no empty-thread create), so
    this composes + sends the proposal as the opening email. ``proposal`` is the
    current best guess (carried from workflow state); the txn facts + category
    names are re-read from YNAB (the source of truth) at compose time. The open
    is idempotent on the per-transaction label, so a retry re-finds the thread
    rather than sending a duplicate.
    """
    import asyncio

    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings

    settings = Settings()
    snapshot, names = await _read_for_compose(ynab_id)
    body = await _compose_body(
        snapshot, proposal, MessagePurpose.PROPOSAL, names
    )
    mail = MailClient.from_env()
    return await asyncio.to_thread(
        mail.open_thread,
        inbox_id=settings.inbox,
        to=list(settings.owners),
        subject=_subject(snapshot),
        body=body,
        txn_label=_txn_label(ynab_id),
    )


@activity.defn
async def send_thread_message(
    ynab_id: str,
    thread_id: str | None,
    purpose: MessagePurpose,
    action_seq: int,
    proposal: Proposal | None,
) -> None:
    """Send a follow-up message on the transaction's thread.

    ``action_seq`` is the per-transaction idempotency key: the send dedups on it
    so a retry never double-sends (SPEC §3). ``proposal`` is the current best
    guess where the purpose needs it (re-proposal); other purposes derive their
    content from a re-read of the YNAB snapshot.
    """
    import asyncio

    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings

    if thread_id is None:
        msg = f"cannot send {purpose.value} for {ynab_id}: no thread open yet"
        raise RuntimeError(msg)
    settings = Settings()
    snapshot, names = await _read_for_compose(ynab_id)
    body = await _compose_body(snapshot, proposal, purpose, names)
    mail = MailClient.from_env()
    await asyncio.to_thread(
        mail.send_on_thread,
        inbox_id=settings.inbox,
        thread_id=thread_id,
        body=body,
        seq_label=_seq_label(ynab_id, action_seq),
    )


@activity.defn
async def interpret_inbound(
    signal: InboundSignal, snapshot: YnabSnapshot
) -> ReplyOutcome:
    """Interpret an inbound reply or matched receipt (answer or question)."""
    raise NotImplementedError(_STUB)


@activity.defn
async def converge(
    snapshot: YnabSnapshot, instruction: InboundSignal
) -> ConvergeOutcome:
    """Converge a REVISING run to its target and verify it (SPEC §3)."""
    raise NotImplementedError(_STUB)


@activity.defn
async def feed_rule_learning(feed: FeedRuleLearning) -> None:
    """Feed a confirm/correct event to rule learning (W5; SPEC §9).

    The real body loads the payee's rules, runs
    :func:`ynab_agent.learn.handler.plan_rule_update`, and persists the result;
    the workflow tests register a mock that drives an in-memory rule store.
    """
    raise NotImplementedError(_STUB)


@activity.defn
async def close_thread(thread_id: str) -> None:
    """Label and close the AgentMail thread on archive."""
    import asyncio

    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings

    settings = Settings()
    mail = MailClient.from_env()
    await asyncio.to_thread(
        mail.close, inbox_id=settings.inbox, thread_id=thread_id
    )
