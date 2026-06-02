"""The receipt-matching agent: which transaction is this receipt? (SPEC §6).

The agentic half of W4's ``match_receipt``. Given a parked receipt's facts and a
short list of candidate transactions, the model reasons over amount, date, and
merchant to decide: a confident single match, an ambiguous several, or none.
:func:`to_match_outcome` maps the model's verdict onto the domain
:data:`~ynab_agent.join.match.MatchOutcome`, defaulting to ``NoMatch`` on any
malformed verdict — the spine handles ``NoMatch`` safely (it parks, re-checks).

The model is injected per run so tests drive a ``TestModel`` offline; production
uses :func:`~ynab_agent.agentic.model.build_model` (Ollama).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field
from pydantic_ai import Agent

from ynab_agent.agentic.model import run_structured
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.ids import YnabTransactionId
from ynab_agent.join.match import (
    Ambiguous,
    ConfidentMatch,
    MatchOutcome,
    NoMatch,
)

if TYPE_CHECKING:
    from pydantic_ai.models import Model

# An ambiguous verdict must name at least this many candidates (SPEC §6).
_MIN_CANDIDATES = 2


class CandidateTxn(Frozen):
    """One transaction the receipt might belong to."""

    id: str
    payee: str
    amount_display: str
    date_display: str


class ReceiptFacts(Frozen):
    """The parsed receipt facts the agent matches against."""

    merchant: str
    total_display: str
    date_display: str | None = None


class MatchRequest(Frozen):
    """A receipt and the candidate transactions to match it to."""

    receipt: ReceiptFacts
    candidates: tuple[CandidateTxn, ...] = Field(min_length=1)


class MatchDecision(StrEnum):
    """The model's matching verdict."""

    MATCH = "match"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"


class MatchVerdict(Frozen):
    """The agent's structured verdict (mapped to a domain MatchOutcome)."""

    decision: MatchDecision
    txn_id: str | None = None
    candidate_ids: tuple[str, ...] = ()


_SYSTEM_PROMPT = """\
You match a forwarded receipt to a bank transaction. You are given the receipt's
merchant, total, and date, and a short list of candidate transactions (each with
an id, payee, amount, and date).

Decide one of: `match` with the single `txn_id` whose amount and merchant
clearly agree (dates within about a day); `ambiguous` with the `candidate_ids`
(two or more) when several plausibly fit and you cannot choose; or `no_match`
when none fits. Every id you return MUST come from the candidates. When unsure,
prefer `ambiguous` or `no_match` over a wrong `match` — a wrong match is worse
than asking."""

_AGENT: Agent[None, MatchVerdict] = Agent(
    output_type=MatchVerdict,
    system_prompt=_SYSTEM_PROMPT,
)


def _format_request(request: MatchRequest) -> str:
    """Render the request as the agent's user prompt."""
    receipt = request.receipt
    lines = [
        f"Receipt merchant: {receipt.merchant}",
        f"Receipt total: {receipt.total_display}",
    ]
    if receipt.date_display:
        lines.append(f"Receipt date: {receipt.date_display}")
    lines.append("Candidate transactions:")
    lines.extend(
        f"  - id {c.id}: {c.payee}, {c.amount_display}, {c.date_display}"
        for c in request.candidates
    )
    return "\n".join(lines)


async def match_receipt(
    request: MatchRequest, *, model: Model | None = None
) -> MatchVerdict:
    """Run the receipt-matching agent for one receipt (SPEC §6).

    Args:
        request: The receipt facts and the candidate transactions.
        model: A model to use; defaults to the configured Ollama/Gemma.

    Returns:
        The agent's structured match verdict.
    """
    return await run_structured(
        _AGENT,
        _format_request(request),
        output_type=MatchVerdict,
        model=model,
    )


def to_match_outcome(verdict: MatchVerdict) -> MatchOutcome:
    """Map the agent's verdict onto a domain MatchOutcome (SPEC §6).

    Falls back to ``NoMatch`` for any verdict that does not satisfy the domain
    invariants (a match without a txn id, an ambiguous with fewer than two
    candidates) — the spine treats ``NoMatch`` as "keep waiting", never a guess.
    """
    if verdict.decision is MatchDecision.MATCH and verdict.txn_id:
        return ConfidentMatch(txn_id=YnabTransactionId(verdict.txn_id))
    if (
        verdict.decision is MatchDecision.AMBIGUOUS
        and len(verdict.candidate_ids) >= _MIN_CANDIDATES
    ):
        return Ambiguous(
            candidates=tuple(
                YnabTransactionId(c) for c in verdict.candidate_ids
            )
        )
    return NoMatch()
