"""The compose agent: write a transaction's email message (SPEC §5).

The agentic half of the mail activities. Given the message PURPOSE plus the
transaction's facts (re-read from YNAB), the current best-guess category, and a
few alternative categories, a Pydantic AI agent writes a short, natural email
BODY to the budget owner. The subject is templated deterministically by the
activity; only the prose is model-written (a loose template — warm, specific,
and it names alternatives so the owner sees what's available).

The model is injected per run so tests drive a ``TestModel``/``FunctionModel``
offline; production uses :func:`~ynab_agent.agentic.model.build_model` (Ollama).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import Agent

from ynab_agent.agentic.model import build_model
from ynab_agent.domain.base import Frozen

if TYPE_CHECKING:
    from pydantic_ai.models import Model


class ComposeRequest(Frozen):
    """The facts the agent writes one transaction email from."""

    purpose: str  # the MessagePurpose value: proposal / confirm / clarify / ...
    payee: str
    amount_display: str
    txn_date: str
    memo: str | None = None
    proposed_category: str | None = None  # the best-guess category NAME
    alternatives: tuple[str, ...] = ()  # other category names to offer
    rationale: str | None = None  # one-line reason for the best guess
    question: str | None = (
        None  # an explicit question, when the purpose has one
    )


_SYSTEM_PROMPT = """\
You write a short, warm email to a person about ONE of their bank transactions,
on behalf of their budgeting assistant. You are given the message PURPOSE, the
transaction facts (payee, amount, date, optional memo), and — for a proposal —
your best-guess category, a one-line rationale, and a few alternatives.

Write ONLY the email body (no subject line, no signature). Keep it brief (2-4
sentences), natural, and specific to this transaction. Match the purpose:

- proposal: name your best-guess category and the one-line reason, then list the
  alternative categories as options, and invite a free-form reply — they can
  say "yes" to confirm, name a different category, or ask a question.
- confirm: confirm the category was set; one friendly sentence.
- clarify: ask the given question plainly (or, if none, ask what category fits).
- fyi / archive_notice / revise_summary / handoff / possibly_inconsistent /
  diverged_readback: a brief, appropriate note for that situation.

Never invent a category that wasn't given to you. Be concise — this lands in a
real inbox."""

_AGENT: Agent[None, str] = Agent(system_prompt=_SYSTEM_PROMPT)


def _format_request(request: ComposeRequest) -> str:
    """Render the request as the agent's user prompt."""
    lines = [
        f"Purpose: {request.purpose}",
        f"Payee: {request.payee}",
        f"Amount: {request.amount_display}",
        f"Date: {request.txn_date}",
    ]
    if request.memo:
        lines.append(f"Memo: {request.memo}")
    if request.proposed_category:
        lines.append(f"Best-guess category: {request.proposed_category}")
    if request.rationale:
        lines.append(f"Why: {request.rationale}")
    if request.alternatives:
        lines.append(
            "Alternative categories: " + ", ".join(request.alternatives)
        )
    if request.question:
        lines.append(f"Question to ask: {request.question}")
    return "\n".join(lines)


async def compose(
    request: ComposeRequest, *, model: Model | None = None
) -> str:
    """Write the email body for one transaction message (SPEC §5).

    Args:
        request: The purpose + transaction facts + proposal/alternatives.
        model: A model to use; defaults to the configured Ollama/Gemma.

    Returns:
        The email body text (the subject is templated by the caller).
    """
    run_model = model if model is not None else build_model()
    result = await _AGENT.run(_format_request(request), model=run_model)
    return result.output
