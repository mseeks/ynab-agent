"""The enrichment agent: propose a category for a transaction (SPEC §4.1).

The agentic half of the ``enrich`` step. Given a transaction's facts and the
candidate budget categories, a Pydantic AI agent picks the best category, rates
its confidence, and explains why. The deterministic gate (``policy.gate``) then
decides auto-apply vs. ask — confidence here is *framing only*, never a gate
(principle 6). :func:`to_proposal` maps the agent's structured output onto the
domain :class:`~ynab_agent.domain.proposal.Proposal`.

The model is injected per run so tests drive a ``TestModel``/``FunctionModel``
offline; production uses :func:`~ynab_agent.agentic.model.build_model` (Ollama).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field
from pydantic_ai import Agent

from ynab_agent.agentic.model import build_model
from ynab_agent.domain.allocations import ProposedCategory
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import Confidence, SourceKind
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.proposal import Proposal, ProposalSource

if TYPE_CHECKING:
    from pydantic_ai.models import Model


class CandidateCategory(Frozen):
    """One budget category the agent may choose from."""

    id: str
    name: str


class EnrichmentRequest(Frozen):
    """The facts the agent reasons over to propose a category."""

    payee: str
    amount_display: str
    candidates: tuple[CandidateCategory, ...] = Field(min_length=1)
    memo: str | None = None
    rule_hint: str | None = None


class EnrichmentSuggestion(Frozen):
    """The agent's structured proposal (mapped to a domain Proposal)."""

    category_id: str
    confidence: Confidence
    rationale: str


_SYSTEM_PROMPT = """\
You categorize a single bank transaction for a personal budget. You are given
the payee, amount, an optional memo, an optional hint from a learned rule, and a
list of candidate budget categories (each with an id and a name).

Choose the SINGLE best category for the transaction. Your `category_id` MUST be
one of the provided candidate ids — never invent one. Rate your confidence
(high / medium / low) and give a one-sentence rationale. Confidence is framing
only; a human or a trusted rule decides whether to auto-apply, not you."""

_AGENT: Agent[None, EnrichmentSuggestion] = Agent(
    output_type=EnrichmentSuggestion,
    system_prompt=_SYSTEM_PROMPT,
)


def _format_request(request: EnrichmentRequest) -> str:
    """Render the request as the agent's user prompt."""
    lines = [
        f"Payee: {request.payee}",
        f"Amount: {request.amount_display}",
    ]
    if request.memo:
        lines.append(f"Memo: {request.memo}")
    if request.rule_hint:
        lines.append(f"Rule hint: {request.rule_hint}")
    lines.append("Candidate categories:")
    lines.extend(f"  - {c.name} (id: {c.id})" for c in request.candidates)
    return "\n".join(lines)


async def propose(
    request: EnrichmentRequest, *, model: Model | None = None
) -> EnrichmentSuggestion:
    """Run the enrichment agent for one transaction (SPEC §4.1).

    Args:
        request: The transaction facts and candidate categories.
        model: A model to use; defaults to the configured Ollama/Gemma.

    Returns:
        The agent's structured category suggestion.
    """
    run_model = model if model is not None else build_model()
    result = await _AGENT.run(_format_request(request), model=run_model)
    return result.output


def to_proposal(suggestion: EnrichmentSuggestion) -> Proposal:
    """Map the agent's suggestion onto a domain Proposal (SPEC §4.1)."""
    return Proposal(
        allocation=ProposedCategory(
            category=CategoryId(suggestion.category_id)
        ),
        confidence=suggestion.confidence,
        rationale=suggestion.rationale,
        sources=(ProposalSource(kind=SourceKind.MODEL),),
    )
