"""The reply-interpreting agent: what did the human mean? (SPEC §3, §5).

The agentic half of W2's ``interpret_inbound``. Given a human's free-text reply
on a transaction's thread, the model reads the *intent*: approve the current
proposal, switch to a different category, or — when unclear — ask one clarifying
question. The model only classifies intent and (for a switch) picks a category;
the deterministic spine assembles the ``Decision`` (stamping the human as
decider and the time), so the model never fabricates an approval timestamp or
an allocation the spine cannot resolve.

:func:`to_reply_outcome` maps the intent onto the domain ``ReplyOutcome``,
defaulting to a clarifying question whenever a switch arrives without a category
— the safe move is always to ask, never to guess a write.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from pydantic import Field
from pydantic_ai import Agent

from ynab_agent.agentic.enrich import CandidateCategory
from ynab_agent.agentic.model import build_model
from ynab_agent.domain.allocations import ResolvedCategory
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import DecidedBy
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.proposal import Decision
from ynab_agent.workflow.types import (
    AnswerOutcome,
    ClarifyOutcome,
    ReplyOutcome,
)

if TYPE_CHECKING:
    import datetime

    from pydantic_ai.models import Model


class ReplyIntent(StrEnum):
    """What a human's reply asked for."""

    APPROVE = "approve"
    RECATEGORIZE = "recategorize"
    CLARIFY = "clarify"


class InterpretRequest(Frozen):
    """A human reply and the context needed to read its intent."""

    reply_text: str
    payee: str
    amount_display: str
    proposed_category_name: str
    candidates: tuple[CandidateCategory, ...] = Field(min_length=1)


class Interpretation(Frozen):
    """The agent's read of the reply (mapped to a domain ReplyOutcome)."""

    intent: ReplyIntent
    category_id: str | None = None
    question: str | None = None


_SYSTEM_PROMPT = """\
You read one reply a human sent about a proposed transaction categorization. You
are given the reply text, the payee and amount, the category currently proposed,
and the candidate categories (id + name).

Classify the reply's intent: `approve` if they accept the current proposal (e.g.
"ok", "yes", "sounds good"); `recategorize` if they want a different category —
then set `category_id` to the matching candidate id; or `clarify` if the reply
is ambiguous or asks a question — then set a short `question` to send back. When
in doubt, prefer `clarify`: asking again is safe, a wrong write is not."""

_AGENT: Agent[None, Interpretation] = Agent(
    output_type=Interpretation,
    system_prompt=_SYSTEM_PROMPT,
)


def _format_request(request: InterpretRequest) -> str:
    """Render the request as the agent's user prompt."""
    lines = [
        f"Reply: {request.reply_text}",
        f"Payee: {request.payee}",
        f"Amount: {request.amount_display}",
        f"Currently proposed: {request.proposed_category_name}",
        "Candidate categories:",
    ]
    lines.extend(f"  - {c.name} (id: {c.id})" for c in request.candidates)
    return "\n".join(lines)


async def interpret(
    request: InterpretRequest, *, model: Model | None = None
) -> Interpretation:
    """Run the reply-interpreting agent for one reply (SPEC §3).

    Args:
        request: The reply text and its categorization context.
        model: A model to use; defaults to the configured Ollama/Gemma.

    Returns:
        The agent's structured reading of the reply.
    """
    run_model = model if model is not None else build_model()
    result = await _AGENT.run(_format_request(request), model=run_model)
    return result.output


def _human_decision(
    category: CategoryId, decided_at: datetime.datetime
) -> Decision:
    return Decision(
        allocation=ResolvedCategory(category=category),
        approved=True,
        decided_by=DecidedBy.HUMAN,
        decided_at=decided_at,
    )


def to_reply_outcome(
    interpretation: Interpretation,
    *,
    proposed_category: CategoryId,
    decided_at: datetime.datetime,
) -> ReplyOutcome:
    """Map the agent's reading onto a domain ReplyOutcome (SPEC §3).

    ``approve`` commits the proposed category; ``recategorize`` commits the
    named one (or asks, if none was given); ``clarify`` sends the question back.
    The spine, not the model, sets the decider and timestamp.
    """
    match interpretation.intent:
        case ReplyIntent.APPROVE:
            return AnswerOutcome(
                decision=_human_decision(proposed_category, decided_at)
            )
        case ReplyIntent.RECATEGORIZE:
            if interpretation.category_id:
                return AnswerOutcome(
                    decision=_human_decision(
                        CategoryId(interpretation.category_id), decided_at
                    )
                )
            return ClarifyOutcome(question="Which category should this be?")
        case ReplyIntent.CLARIFY:
            return ClarifyOutcome(
                question=interpretation.question
                or "Could you clarify how you'd like this categorized?"
            )
    assert_never(interpretation.intent)
