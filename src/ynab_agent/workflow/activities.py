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

from ynab_agent.domain.effects import FeedRuleLearning, MessagePurpose
from ynab_agent.domain.events import ConvergeOutcome, EnrichmentOutcome
from ynab_agent.domain.proposal import Decision
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
    """Read the current YNAB snapshot, or ``None`` if not yet imported."""
    raise NotImplementedError(_STUB)


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
async def commit_to_ynab(decision: Decision) -> None:
    """Commit a decision to YNAB (the deterministic write)."""
    raise NotImplementedError(_STUB)


@activity.defn
async def read_back(ynab_id: str) -> TargetState | None:
    """Read the post-write end-state for verification, or ``None`` if unread."""
    raise NotImplementedError(_STUB)


@activity.defn
async def open_thread(ynab_id: str) -> str:
    """Create the AgentMail thread; returns its id."""
    raise NotImplementedError(_STUB)


@activity.defn
async def send_thread_message(
    thread_id: str | None, purpose: MessagePurpose, action_seq: int
) -> None:
    """Send a message on the transaction's thread.

    ``action_seq`` is the per-transaction idempotency key: the implementation
    must dedup on it so a retry never double-sends (SPEC §3).
    """
    raise NotImplementedError(_STUB)


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
    raise NotImplementedError(_STUB)
