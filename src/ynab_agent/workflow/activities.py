"""The I/O ports of the transaction lifecycle, as Temporal activities.

Every side effect the workflow performs — reading YNAB, committing a write,
sending email, the agentic enrichment/interpretation/converge steps — is an
activity, so neither the model's nor the spine's I/O runs in workflow code
(SPEC §0.5). All of them are fully wired: YNAB over its REST client, email
over AgentMail, the agentic steps over Pydantic AI + Ollama/Gemma. The heavy
clients are imported lazily inside the bodies so they never enter a workflow
sandbox; the workflow tests register mock implementations against the same
signatures.
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


async def _load_payee_rules(payee: str) -> list[Rule]:
    """Query the durable rule registry for this payee's rules (W5, SPEC §14).

    Returns ``[]`` when the registry has not been started yet (no learning has
    happened) or is unreachable — the conservative fallback, which routes the
    transaction to ASK rather than risking an auto-apply on stale knowledge.
    """
    from temporalio.service import RPCError

    from ynab_agent.domain.rule import Rule
    from ynab_agent.workflow.registry_types import REGISTRY_WORKFLOW_ID
    from ynab_agent.workflow.temporal_client import client

    temporal = await client()
    handle = temporal.get_workflow_handle(REGISTRY_WORKFLOW_ID)
    try:
        # ``result_type`` is load-bearing: the pydantic data converter decodes a
        # query payload to plain ``dict``s without it, and the gate reads real
        # ``Rule`` objects (``rule.match``) — a dict there raises AttributeError
        # and the enrich activity retries forever (SPEC §14 gate-load path).
        rules = await handle.query(
            "payee_rules", payee, result_type=tuple[Rule, ...]
        )
    except RPCError:
        return []
    return list(rules)


async def _load_auto_action_counters(
    now: datetime.datetime,
) -> AutoActionCounters:
    """Read the live circuit-breaker counts from the durable ledger (SPEC §0.6).

    Returns zeros when the ledger has not been started yet (no auto-action has
    ever happened) or is unreachable — the correct conservative default *here*,
    since a never-started ledger genuinely means zero auto-actions and the
    breaker must not trip on its own absence (contrast ``_load_payee_rules``,
    which fails *closed* to ASK because an unknown rule must never auto-apply).
    """
    from temporalio.service import RPCError

    from ynab_agent.policy.floor import AutoActionCounters
    from ynab_agent.workflow.auto_action_types import (
        AUTO_ACTION_LEDGER_WORKFLOW_ID,
        CountersRequest,
    )
    from ynab_agent.workflow.temporal_client import client

    temporal = await client()
    handle = temporal.get_workflow_handle(AUTO_ACTION_LEDGER_WORKFLOW_ID)
    try:
        result: AutoActionCounters = await handle.query(
            "counters",
            CountersRequest(now=now),
            result_type=AutoActionCounters,
        )
    except RPCError:
        return AutoActionCounters()
    return result


async def _load_enrichment_inputs(
    snapshot: YnabSnapshot,
    now: datetime.datetime,
) -> tuple[tuple[CandidateCategory, ...], list[Rule], AutoActionCounters]:
    """Fetch the candidate categories, in-scope rules, and live auto counters.

    The candidates are the budget's live categories, read from YNAB (the source
    of truth); the rules come from the durable registry, keyed on the payee; the
    auto-action counters come from the durable circuit-breaker ledger (SPEC
    §0.6), so the per-run / per-day cap reads real counts and can trip. The gate
    (SPEC §4.2, §14) decides auto-vs-ask over the loaded rules — auto-applying
    only a blessed one, still bounded by the floor.
    """
    import asyncio

    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    spends = await asyncio.to_thread(client.category_spends)
    rules = await _load_payee_rules(snapshot.payee)
    counters = await _load_auto_action_counters(now)
    return _candidates_from_spends(spends), rules, counters


@activity.defn
async def enrich(snapshot: YnabSnapshot) -> EnrichmentOutcome:
    """Assemble the proposal and route via the gate (the agentic middle).

    The model stack is imported lazily, here in the activity body, so it never
    enters the workflow sandbox. The gate decides autonomy from the rules; the
    agent runs only to produce the proposal a human is asked to confirm.
    """
    from datetime import UTC, datetime

    from ynab_agent.agentic.enrich import decide_enrichment

    now = datetime.now(UTC)
    candidates, rules, counters = await _load_enrichment_inputs(snapshot, now)
    return await decide_enrichment(
        snapshot, candidates, rules, counters, now=now
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


def _decided_display(
    decision: Decision | None, names: dict[str, str]
) -> str | None:
    """The category name(s) a decision wrote, for naming it in an email."""
    if decision is None:
        return None
    allocation = decision.allocation
    if isinstance(allocation, ResolvedCategory):
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


def _diverged_detail(
    snapshot: YnabSnapshot, decision: Decision, names: dict[str, str]
) -> str:
    """A which-wins comparison from the live snapshot vs. the intended write.

    The converge path carries its own comparison on the effect; this covers the
    plain commit→verify path, where only the intended decision is known — the
    current side comes from the fresh snapshot read at send time.
    """
    current = (
        names.get(str(snapshot.category_id), str(snapshot.category_id))
        if snapshot.category_id is not None
        else ("a split" if snapshot.subtransactions else "uncategorized")
    )
    intended = _decided_display(decision, names)
    return (
        f"YNAB now shows {current}, but I set {intended} — which should "
        "win? Reply with your choice and I'll sort it out."
    )


def _render_message(
    snapshot: YnabSnapshot,
    proposal: Proposal | None,
    purpose: MessagePurpose,
    names: dict[str, str],
    *,
    detail: str | None = None,
    decision: Decision | None = None,
) -> tuple[str, str]:
    """Lay out one message for the thread as ``(text, html)`` (SPEC §5).

    Deterministic. The proposal's category + alternatives (model-chosen
    upstream) are resolved to names here and handed to the template — no model
    call at send time. ``detail`` and ``decision`` carry the message-specific
    payload from the state machine (the clarify question, the diverged
    comparison, what a confirm/FYI/revision actually wrote). The two parts are
    rendered from the same request, so the words can never differ.
    """
    from ynab_agent.agentic.compose import (
        ComposeRequest,
        render_body,
        render_body_html,
    )

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
    if (
        detail is None
        and decision is not None
        and purpose is MessagePurpose.DIVERGED_READBACK
    ):
        detail = _diverged_detail(snapshot, decision, names)
    request = ComposeRequest(
        purpose=purpose.value,
        payee=snapshot.payee,
        amount_display=str(snapshot.amount),
        txn_date=_date_display(snapshot.txn_date),
        memo=snapshot.memo,
        proposed_category=proposed,
        alternatives=alternatives,
        rationale=proposal.rationale if proposal is not None else None,
        detail=detail,
        decided_category=_decided_display(decision, names),
    )
    return render_body(request), render_body_html(request)


@activity.defn
async def open_thread(
    ynab_id: str,
    proposal: Proposal | None,
    purpose: MessagePurpose = MessagePurpose.PROPOSAL,
    detail: str | None = None,
    decision: Decision | None = None,
) -> str:
    """Open the AgentMail thread with its first message; returns its id.

    A thread starts on its first send (AgentMail has no empty-thread create).
    Usually that first message is the proposal, but an auto-applied transaction
    has no proposal email — its thread opens with the FYI (``purpose``/
    ``decision``), which is what makes the SPEC §14.5 per-action FYI + one-reply
    undo possible at all. The txn facts + category names are re-read from YNAB
    at compose time, and the open is idempotent on the per-transaction label, so
    a retry re-finds the thread rather than sending a duplicate.
    """
    import asyncio

    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings

    settings = Settings()
    snapshot, names = await _read_for_compose(ynab_id)
    body, html = _render_message(
        snapshot, proposal, purpose, names, detail=detail, decision=decision
    )
    headline = _decided_display(decision, names) or (
        _allocation_display(proposal.allocation, names)
        if proposal is not None
        else None
    )
    mail = MailClient.from_env()
    return await asyncio.to_thread(
        mail.open_thread,
        inbox_id=settings.inbox,
        to=list(settings.owners),
        subject=_subject(snapshot, headline),
        body=body,
        txn_label=_txn_label(ynab_id),
        html=html,
    )


@activity.defn
async def send_thread_message(
    ynab_id: str,
    thread_id: str | None,
    purpose: MessagePurpose,
    action_seq: int,
    proposal: Proposal | None,
    detail: str | None = None,
    decision: Decision | None = None,
) -> None:
    """Send a follow-up message on the transaction's thread.

    ``action_seq`` is the per-transaction idempotency key: the send dedups on it
    so a retry never double-sends (SPEC §3). ``proposal`` is the current best
    guess where the purpose needs it (re-proposal); ``detail``/``decision``
    carry the message's specific payload (clarify question, diverged
    comparison, what was written). Recipients are set to the owners explicitly:
    when the agent was the last speaker on the thread, AgentMail would
    otherwise address the reply back to the agent's own inbox and the owners
    would never see it (the same fix the W7 balancer needed, #17).
    """
    import asyncio

    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings

    if thread_id is None:
        msg = f"cannot send {purpose.value} for {ynab_id}: no thread open yet"
        raise RuntimeError(msg)
    settings = Settings()
    snapshot, names = await _read_for_compose(ynab_id)
    body, html = _render_message(
        snapshot, proposal, purpose, names, detail=detail, decision=decision
    )
    mail = MailClient.from_env()
    await asyncio.to_thread(
        mail.send_on_thread,
        inbox_id=settings.inbox,
        thread_id=thread_id,
        body=body,
        seq_label=_seq_label(ynab_id, action_seq),
        to=list(settings.owners),
        html=html,
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
    the human + time into the resulting Decision. A non-reply inbound can't be
    answered directly, so ask rather than guess a write. A *missing* proposal
    (a flagged verify-failure entry, a NeedsHuman wait) is fine: the owner can
    still name a category outright, and looping the same canned question at
    them made those states unanswerable by email. Names + candidates are
    re-read from YNAB at interpret time.
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
    if not isinstance(signal, ReplySignal):
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
        proposed_category_name=names.get(str(proposed_id), str(proposed_id))
        if proposed_id is not None
        else None,
        candidates=_candidates_from_spends(spends),
    )
    interpretation = await interpret(request)
    return to_reply_outcome(
        interpretation,
        proposed_category=proposed_id,
        decided_at=datetime.now(UTC),
        candidates=request.candidates,
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
    snapshot: YnabSnapshot,
    instruction: InboundSignal,
    prior: Decision | None,
) -> ConvergeOutcome:
    """Converge a REVISING run to its target and verify it (SPEC §3 rules 3-4).

    The agent reads the revision instruction into a target (retarget / memo /
    no-change). The spine then converges to it: it reads the *current* YNAB
    end-state first and, comparing against the agent's last-applied decision
    (``prior``), skips a needless write (the no-op exit), adopts a write that
    already landed, or surfaces a divergence (a spouse edited it directly) —
    *before* overwriting it — rather than committing blind and only noticing on
    the read-back. Only a genuine change writes, then verifies field-by-field. A
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
        PrecommitAction,
        classify_verify,
        precommit_action,
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
    candidates = _candidates_from_spends(spends)
    target = await interpret_revision(
        RevisionRequest(
            instruction=instruction.text,
            current_category_name=current_name,
            candidates=candidates,
            current_memo=snapshot.memo,
        )
    )
    plan = to_revision_plan(target, candidates)
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
    end_state = target_of(decision)
    prior_state = target_of(prior) if prior is not None else None

    # SPEC §3 r3-4: read the current end-state *before* writing, then decide.
    current = await asyncio.to_thread(client.read_back, snapshot.ynab_id)
    action = precommit_action(current, end_state, prior_state)
    if action is PrecommitAction.NO_CHANGE:
        return NoChange()
    if action is PrecommitAction.ALREADY_TARGET:
        return Reapplied(decision=decision)
    if action is PrecommitAction.DIVERGED:
        return Diverged(
            ynab_summary=_target_summary(current, names),
            requested_summary=_target_summary(end_state, names),
        )

    # PrecommitAction.WRITE: converge to the target, then verify (SPEC §3 r3-4).
    await asyncio.to_thread(client.commit, snapshot.ynab_id, decision)
    read = await asyncio.to_thread(client.read_back, snapshot.ynab_id)
    verdict = classify_verify(read, end_state)
    if verdict is VerifyOutcome.MATCH:
        return Reapplied(decision=decision)
    if verdict is VerifyOutcome.COULD_NOT_CONFIRM:
        return CouldNotConfirm()
    return Diverged(
        ynab_summary=_target_summary(read, names),
        requested_summary=_target_summary(end_state, names),
    )


@activity.defn
async def feed_rule_learning(feed: FeedRuleLearning) -> None:
    """Persist a confirm/correct event into the durable rule registry (W5).

    Signal-with-start on the singleton :class:`RuleRegistryWorkflow`: the first
    learning event creates it, every later one just delivers the signal, and the
    workflow folds the event into the rule table the autonomy gate reads (SPEC
    §9, §14). The conflict policy reuses the running singleton rather than
    starting a second registry.
    """
    from temporalio.common import WorkflowIDConflictPolicy

    from ynab_agent.workflow.registry_types import (
        REGISTRY_WORKFLOW_ID,
        RegistryParams,
    )
    from ynab_agent.workflow.temporal_client import client, task_queue

    temporal = await client()
    await temporal.start_workflow(
        "RuleRegistryWorkflow",
        RegistryParams(),
        id=REGISTRY_WORKFLOW_ID,
        task_queue=task_queue(),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="record",
        start_signal_args=[feed],
    )


@activity.defn
async def record_auto_action(ynab_id: str) -> None:
    """Record a landed auto-action in the durable breaker ledger (SPEC §0.6).

    Signal-with-start on the singleton ledger: the first auto-action creates it,
    every later one just delivers the signal, and the ledger folds it into the
    counts the hard floor reads (mirrors ``feed_rule_learning`` talking to the
    registry). Best-effort — a ledger hiccup must never block or fail the
    categorization it bounds, so any error is logged and swallowed. The breaker
    tolerates an occasional missed count; the per-txn ceiling still binds, and
    the ``ynab_id`` key dedups a retry.
    """
    from temporalio.common import WorkflowIDConflictPolicy

    from ynab_agent.workflow.auto_action_types import (
        AUTO_ACTION_LEDGER_WORKFLOW_ID,
        LedgerParams,
    )
    from ynab_agent.workflow.temporal_client import client, task_queue

    try:
        temporal = await client()
        await temporal.start_workflow(
            "AutoActionLedgerWorkflow",
            LedgerParams(),
            id=AUTO_ACTION_LEDGER_WORKFLOW_ID,
            task_queue=task_queue(),
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            start_signal="record",
            start_signal_args=[ynab_id],
        )
    except Exception:
        activity.logger.warning(
            "auto-action ledger record failed (best-effort): %s", ynab_id
        )


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
