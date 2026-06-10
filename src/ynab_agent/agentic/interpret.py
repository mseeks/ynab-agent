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

import re
from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from pydantic import Field
from pydantic_ai import Agent

from ynab_agent.agentic.enrich import CandidateCategory
from ynab_agent.agentic.model import run_structured
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
    """A human reply and the context needed to read its intent.

    ``proposed_category_name`` is ``None`` when there is no live proposal (a
    flagged verify-failure entry, a NeedsHuman wait). The owner can still name
    a category outright; only a bare "approve" has nothing to approve then.
    """

    reply_text: str
    payee: str
    amount_display: str
    proposed_category_name: str | None = None
    candidates: tuple[CandidateCategory, ...] = Field(min_length=1)


class Interpretation(Frozen):
    """The agent's read of the reply (mapped to a domain ReplyOutcome)."""

    intent: ReplyIntent
    category_id: str | None = None
    question: str | None = None
    memo: str | None = None


_SYSTEM_PROMPT = """\
You read one reply a human sent about a proposed transaction categorization. You
are given the candidate categories (id + name), the payee and amount, the
category currently proposed, and finally the reply text itself.

Classify the reply's intent: `approve` if they accept the current proposal (e.g.
"ok", "yes", "sounds good"); `recategorize` if they name or describe a category
— then set `category_id` to the matching candidate id; or `clarify` if the reply
is ambiguous or asks a question — then set a short `question` to send back. When
in doubt, prefer `clarify`: asking again is safe, a wrong write is not.

There may be NO current proposal (shown as "(none)"): the agent asked an open
question rather than proposing. A bare yes/ok then has nothing to approve —
classify it `clarify`; a reply that names a category is still `recategorize`.

Also set `memo`: if the reply gives any *context or reasoning* beyond the bare
category — what it was for, who it was for, an occasion ("gift for mom", "kids'
soccer", "their shopping") — distil it into a short, factual memo (≤ a sentence)
that will be saved on the transaction. If the reply is only a bare confirmation
or category with no added context, leave `memo` null. Never invent detail."""

_AGENT: Agent[None, Interpretation] = Agent(
    output_type=Interpretation,
    system_prompt=_SYSTEM_PROMPT,
)


def _format_request(request: InterpretRequest) -> str:
    """Render the request as the agent's user prompt.

    Ordered for KV prefix-cache reuse: the candidate category list is
    byte-stable across calls, so it leads and extends the shared prefix the
    server can skip re-prefilling; the per-call facts follow, with the
    reply — the most variable line — last (which also puts it nearest the
    generation point).
    """
    lines = ["Candidate categories:"]
    lines.extend(f"  - {c.name} (id: {c.id})" for c in request.candidates)
    lines.append(f"Payee: {request.payee}")
    lines.append(f"Amount: {request.amount_display}")
    lines.append(
        f"Currently proposed: {request.proposed_category_name or '(none)'}"
    )
    lines.append(f"Reply: {request.reply_text}")
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
    return await run_structured(
        _AGENT,
        _format_request(request),
        output_type=Interpretation,
        model=model,
    )


def _supported_memo(memo: str | None, reply_text: str) -> str | None:
    """Keep a memo only when the reply actually contains its substance.

    The memo is *defined* as context distilled from the reply ("never invent
    detail"), but the model occasionally fabricates one anyway — e.g. echoing
    a category name — and a fabricated memo overwrites real YNAB detail (an
    Amazon item list, gone). Deterministic guard: at least one substantive
    word of the memo must appear in the reply, else the memo is dropped (the
    safe loss — a nice-to-have note — over the unsafe write).
    """
    if memo is None:
        return None
    words = re.findall(r"[A-Za-z0-9']{3,}", memo.lower())
    if not words:
        return None
    reply = reply_text.lower()
    return memo if any(word in reply for word in words) else None


def _human_decision(
    category: CategoryId,
    decided_at: datetime.datetime,
    *,
    memo: str | None = None,
) -> Decision:
    return Decision(
        allocation=ResolvedCategory(category=category),
        memo=memo,
        approved=True,
        decided_by=DecidedBy.HUMAN,
        decided_at=decided_at,
    )


def to_reply_outcome(
    interpretation: Interpretation,
    *,
    proposed_category: CategoryId | None,
    decided_at: datetime.datetime,
    candidates: tuple[CandidateCategory, ...],
    reply_text: str,
) -> ReplyOutcome:
    """Map the agent's reading onto a domain ReplyOutcome (SPEC §3, §14.4).

    ``approve`` commits the proposed category (or asks, when nothing was
    proposed — there is nothing to approve); ``recategorize`` commits the named
    one (or asks, if none was given *or* the model invented an id — a write
    against a hallucinated category would land wrong or 400); ``clarify`` sends
    the question back. Any rationale the reply carried rides along as the
    decision's ``memo`` (the spine writes it to YNAB) — but only when the
    reply actually contains it (:func:`_supported_memo`). The spine, not the
    model, sets decider and time.
    """
    memo = _supported_memo(interpretation.memo, reply_text)
    match interpretation.intent:
        case ReplyIntent.APPROVE:
            if proposed_category is None:
                return ClarifyOutcome(
                    question=(
                        "There's no pending suggestion to approve here — "
                        "which category should this be?"
                    )
                )
            return AnswerOutcome(
                decision=_human_decision(
                    proposed_category, decided_at, memo=memo
                )
            )
        case ReplyIntent.RECATEGORIZE:
            if interpretation.category_id and any(
                c.id == interpretation.category_id for c in candidates
            ):
                return AnswerOutcome(
                    decision=_human_decision(
                        CategoryId(interpretation.category_id),
                        decided_at,
                        memo=memo,
                    )
                )
            if interpretation.category_id:
                return ClarifyOutcome(
                    question=(
                        "I couldn't match that to one of the budget's "
                        "categories — could you give the category name as "
                        "it appears in YNAB?"
                    )
                )
            return ClarifyOutcome(question="Which category should this be?")
        case ReplyIntent.CLARIFY:
            return ClarifyOutcome(
                question=interpretation.question
                or "Could you clarify how you'd like this categorized?"
            )
    assert_never(interpretation.intent)
