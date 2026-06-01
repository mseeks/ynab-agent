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

from ynab_agent.domain.allocations import (
    ProposedCategory,
    ProposedSplit,
    ResolvedCategory,
)
from ynab_agent.domain.effects import FeedRuleLearning, MessagePurpose
from ynab_agent.domain.events import ConvergeOutcome, EnrichmentOutcome
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.proposal import Decision, Proposal
from ynab_agent.domain.signals import InboundSignal
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.policy.converge import TargetState
from ynab_agent.workflow.types import ReplyOutcome

if TYPE_CHECKING:
    import datetime

    # Annotation-only: never imported at runtime, so the agentic/model stack
    # (pydantic-ai) never enters the workflow sandbox. The enrich body imports
    # it lazily, inside the activity, where it runs outside the sandbox.
    from ynab_agent.agentic.enrich import CandidateCategory
    from ynab_agent.budget.overspend import CategorySpend
    from ynab_agent.domain.rule import Rule
    from ynab_agent.policy.floor import AutoActionCounters


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


def _candidates_from_spends(
    spends: tuple[CategorySpend, ...],
) -> tuple[CandidateCategory, ...]:
    """Map the budget's live categories to the agent's candidate choices."""
    from ynab_agent.agentic.enrich import CandidateCategory

    return tuple(
        CandidateCategory(id=str(spend.category), name=spend.name)
        for spend in spends
    )


async def _load_enrichment_inputs(
    snapshot: YnabSnapshot,
) -> tuple[tuple[CandidateCategory, ...], list[Rule], AutoActionCounters]:
    """Fetch the candidate categories, in-scope rules, and auto counters.

    The candidates are the budget's live categories, read from YNAB (the source
    of truth). v1 is Gemma-only: there is no rule store yet and the auto-action
    counters start at zero, so the gate always asks a human — rule learning and
    the autonomy ramp arrive in later stages (SPEC §4.2, §9).
    """
    import asyncio

    from ynab_agent.policy.floor import AutoActionCounters
    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    spends = await asyncio.to_thread(client.category_spends)
    rules: list[Rule] = []
    return _candidates_from_spends(spends), rules, AutoActionCounters()


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


def _subject(snapshot: YnabSnapshot, category: str | None) -> str:
    """The thread's subject: payee + amount, and the suggested category.

    No ``[YNAB]`` prefix — the sender address is a known contact. Naming the
    proposed category lets the owner act from the subject line alone.
    """
    base = f"{snapshot.payee} — {snapshot.amount}"
    return f"{base} · {category}?" if category else base


def _date_display(day: datetime.date) -> str:
    """A short, friendly transaction date, e.g. ``May 29`` (no leading zero)."""
    return f"{day.strftime('%b')} {day.day}"


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


def _render_message(
    snapshot: YnabSnapshot,
    proposal: Proposal | None,
    purpose: MessagePurpose,
    names: dict[str, str],
) -> str:
    """Lay out one message body for the thread (deterministic; SPEC §5).

    The proposal's category + alternatives (model-chosen upstream) are resolved
    to names here and handed to the template — no model call at send time.
    """
    from ynab_agent.agentic.compose import ComposeRequest, render_body

    proposed = (
        _allocation_display(proposal.allocation, names)
        if proposal is not None
        else None
    )
    alternatives = (
        tuple(names.get(str(alt), str(alt)) for alt in proposal.alternatives)
        if proposal is not None
        else ()
    )
    request = ComposeRequest(
        purpose=purpose.value,
        payee=snapshot.payee,
        amount_display=str(snapshot.amount),
        txn_date=_date_display(snapshot.txn_date),
        memo=snapshot.memo,
        proposed_category=proposed,
        alternatives=alternatives,
        rationale=proposal.rationale if proposal is not None else None,
    )
    return render_body(request)


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
    body = _render_message(snapshot, proposal, MessagePurpose.PROPOSAL, names)
    proposed = (
        _allocation_display(proposal.allocation, names)
        if proposal is not None
        else None
    )
    mail = MailClient.from_env()
    return await asyncio.to_thread(
        mail.open_thread,
        inbox_id=settings.inbox,
        to=list(settings.owners),
        subject=_subject(snapshot, proposed),
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
    body = _render_message(snapshot, proposal, purpose, names)
    mail = MailClient.from_env()
    await asyncio.to_thread(
        mail.send_on_thread,
        inbox_id=settings.inbox,
        thread_id=thread_id,
        body=body,
        seq_label=_seq_label(ynab_id, action_seq),
    )


def _proposed_category_id(proposal: Proposal | None) -> CategoryId | None:
    """The single proposed category id, or None (a split is not approvable)."""
    if proposal is None:
        return None
    allocation = proposal.allocation
    if isinstance(allocation, ProposedCategory):
        return allocation.category
    return None


@activity.defn
async def interpret_inbound(
    signal: InboundSignal,
    snapshot: YnabSnapshot,
    proposal: Proposal | None,
) -> ReplyOutcome:
    """Interpret a human reply into an answer or a question (SPEC §3, §5).

    Free-form: the model reads whether the reply approves the proposal, names a
    different category, or asks a question. The spine — not the model — stamps
    the human + time into the resulting Decision. A non-reply inbound, or a
    missing/split proposal, can't be answered directly, so ask rather than guess
    a write. Names + candidates are re-read from YNAB at interpret time.
    """
    import asyncio
    from datetime import UTC, datetime

    from ynab_agent.agentic.interpret import (
        InterpretRequest,
        interpret,
        to_reply_outcome,
    )
    from ynab_agent.domain.signals import ReplySignal
    from ynab_agent.workflow.types import ClarifyOutcome
    from ynab_agent.ynab.client import YnabClient

    proposed_id = _proposed_category_id(proposal)
    if not isinstance(signal, ReplySignal) or proposed_id is None:
        return ClarifyOutcome(
            question="Could you say which category this should be?"
        )

    client = YnabClient.from_env()
    spends = await asyncio.to_thread(client.category_spends)
    names = {str(spend.category): spend.name for spend in spends}
    request = InterpretRequest(
        reply_text=signal.text,
        payee=snapshot.payee,
        amount_display=str(snapshot.amount),
        proposed_category_name=names.get(str(proposed_id), str(proposed_id)),
        candidates=_candidates_from_spends(spends),
    )
    interpretation = await interpret(request)
    return to_reply_outcome(
        interpretation,
        proposed_category=proposed_id,
        decided_at=datetime.now(UTC),
    )


def _target_summary(target: TargetState | None, names: dict[str, str]) -> str:
    """A short human description of an end-state, for a divergence note."""
    if target is None:
        return "(could not read)"
    allocation = target.allocation
    if isinstance(allocation, ResolvedCategory):
        label = names.get(str(allocation.category), str(allocation.category))
    else:
        label = "a split"
    return f"{label} — {target.memo}" if target.memo else label


@activity.defn
async def converge(
    snapshot: YnabSnapshot, instruction: InboundSignal
) -> ConvergeOutcome:
    """Converge a REVISING run to its target and verify it (SPEC §3).

    The agent reads the revision instruction into a target (retarget / memo /
    no-change); the spine commits, then reads back and classifies the result. A
    reconciled or closed-month transaction, a non-reply instruction, or anything
    the model under-specifies routes to a human rather than a silent edit.
    """
    import asyncio
    from datetime import UTC, datetime

    from ynab_agent.agentic.converge import (
        RevisionRequest,
        interpret_revision,
        to_revision_plan,
    )
    from ynab_agent.domain.enums import DecidedBy
    from ynab_agent.domain.events import (
        CouldNotConfirm,
        Diverged,
        NeedsHuman,
        NoChange,
        Reapplied,
        VerifyOutcome,
    )
    from ynab_agent.domain.signals import ReplySignal
    from ynab_agent.policy.converge import (
        classify_verify,
        reconciliation_blocks,
        target_of,
    )
    from ynab_agent.ynab.client import YnabClient

    if reconciliation_blocks(snapshot):
        return NeedsHuman(
            reason="reconciled or closed-month — propose, don't silently edit"
        )
    if not isinstance(instruction, ReplySignal):
        return NeedsHuman(reason="non-reply revision instruction unsupported")

    client = YnabClient.from_env()
    spends = await asyncio.to_thread(client.category_spends)
    names = {str(spend.category): spend.name for spend in spends}
    current_name = (
        names.get(str(snapshot.category_id), str(snapshot.category_id))
        if snapshot.category_id is not None
        else "(uncategorized)"
    )
    target = await interpret_revision(
        RevisionRequest(
            instruction=instruction.text,
            current_category_name=current_name,
            candidates=_candidates_from_spends(spends),
            current_memo=snapshot.memo,
        )
    )
    plan = to_revision_plan(target)
    if not plan.changes:
        return NoChange()

    category = (
        CategoryId(plan.category_id)
        if plan.category_id is not None
        else snapshot.category_id
    )
    if category is None:
        return NeedsHuman(reason="revision did not resolve a category")
    decision = Decision(
        allocation=ResolvedCategory(category=category),
        memo=plan.memo if plan.memo is not None else snapshot.memo,
        approved=True,
        decided_by=DecidedBy.HUMAN,
        decided_at=datetime.now(UTC),
    )
    await asyncio.to_thread(client.commit, snapshot.ynab_id, decision)
    read = await asyncio.to_thread(client.read_back, snapshot.ynab_id)
    verdict = classify_verify(read, target_of(decision))
    if verdict is VerifyOutcome.MATCH:
        return Reapplied(decision=decision)
    if verdict is VerifyOutcome.COULD_NOT_CONFIRM:
        return CouldNotConfirm()
    return Diverged(
        ynab_summary=_target_summary(read, names),
        requested_summary=_target_summary(target_of(decision), names),
    )


@activity.defn
async def feed_rule_learning(feed: FeedRuleLearning) -> None:
    """Feed a confirm/correct event to rule learning (W5; SPEC §9).

    No-op in v1: there is no rule store yet (Gemma-only), so confirm/correct
    events are not learned from and every transaction is proposed to a human.
    Rule learning + the autonomy ramp arrive in a later stage; the W2 still
    emits the event so the wiring is in place for when this body lands.
    """
    return None


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
