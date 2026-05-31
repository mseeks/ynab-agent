"""The I/O ports of the transaction lifecycle, as Temporal activities.

Every side effect the workflow performs — reading YNAB, committing a write,
sending email, the agentic enrichment/interpretation/converge steps — is an
activity, so neither the model's nor the spine's I/O runs in workflow code
(SPEC §0.5). These are *stubs*: typed signatures with no body. The real
implementations (YNAB/AgentMail MCP, Pydantic AI) are wired in a later step; the
workflow tests register mock implementations.
"""

from __future__ import annotations

from temporalio import activity

from ynab_agent.domain.effects import MessagePurpose, RuleLearningKind
from ynab_agent.domain.events import ConvergeOutcome, EnrichmentOutcome
from ynab_agent.domain.proposal import Decision
from ynab_agent.domain.signals import InboundSignal
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.policy.converge import TargetState
from ynab_agent.workflow.types import ReplyOutcome

_STUB = "workflow activity stub — register a real or mock implementation"


@activity.defn
async def fetch_snapshot(ynab_id: str) -> YnabSnapshot | None:
    """Read the current YNAB snapshot, or ``None`` if not yet imported."""
    raise NotImplementedError(_STUB)


@activity.defn
async def enrich(snapshot: YnabSnapshot) -> EnrichmentOutcome:
    """Assemble the proposal and route via the gate (the agentic middle)."""
    raise NotImplementedError(_STUB)


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
async def feed_rule_learning(
    event: RuleLearningKind,
    decision: Decision | None,
    prior: Decision | None,
) -> None:
    """Feed a confirm/correct event to rule learning (W5)."""
    raise NotImplementedError(_STUB)


@activity.defn
async def close_thread(thread_id: str) -> None:
    """Label and close the AgentMail thread on archive."""
    raise NotImplementedError(_STUB)
