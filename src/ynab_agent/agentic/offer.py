"""The offer-reply agent: reading a yes/no to an autonomy offer (SPEC §14.7 3b).

The agentic half of the proactive eligibility offer. When a learned rule earns
eligibility, the agent emails the owner a one-time "want me to auto-handle
*Payee* from now on?" question; this reads their free-form reply into an
:class:`~ynab_agent.domain.enums.OfferVerdict` — ``accept`` (bless the rule),
``decline`` (keep proposing), or ``unclear``. It is a real model call, not a
keyword match, so "sure, go ahead", "nah, keep asking me", and the like all read
correctly. Granting standing autonomy is consequential, so the prompt — and
:func:`to_verdict` — default to ``unclear`` on any doubt, never ``accept``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import Agent

from ynab_agent.agentic.model import run_structured
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import OfferVerdict

if TYPE_CHECKING:
    from pydantic_ai.models import Model


class OfferReplyRequest(Frozen):
    """An owner's reply to an autonomy offer, plus the payee it was about."""

    reply_text: str
    payee: str


class OfferReading(Frozen):
    """The agent's read of the reply (mapped to an OfferVerdict)."""

    verdict: OfferVerdict


_SYSTEM_PROMPT = """\
You read one reply an account owner sent in answer to a yes/no question from a
budgeting agent. The agent asked whether it may, from now on, automatically
categorize a particular merchant the same way it has been — granting it standing
autonomy for that payee.

Decide what the reply means:
  - `accept` — a clear yes ("yes", "sure, go ahead", "please do", "sounds good",
    "do it"). Only choose this for an unambiguous agreement to take it over.
  - `decline` — a clear no, or a preference to keep being asked ("no", "nope",
    "keep asking me", "I'll keep approving these myself").
  - `unclear` — anything else: a question, a different topic, a category
    correction, a conditional answer, or anything you are not sure about.

Granting standing autonomy is consequential, so when in doubt choose `unclear`,
never `accept`."""

_AGENT: Agent[None, OfferReading] = Agent(
    output_type=OfferReading,
    system_prompt=_SYSTEM_PROMPT,
)


def _format_request(request: OfferReplyRequest) -> str:
    """Render the request as the agent's user prompt."""
    return (
        f"The offer was about auto-handling: {request.payee}\n"
        f"Their reply: {request.reply_text}"
    )


async def interpret_offer(
    request: OfferReplyRequest, *, model: Model | None = None
) -> OfferReading:
    """Run the offer-reply agent for one message (SPEC §14.7 3b)."""
    return await run_structured(
        _AGENT,
        _format_request(request),
        output_type=OfferReading,
        model=model,
    )


def to_verdict(reading: OfferReading) -> OfferVerdict:
    """The reply's verdict — the deterministic seam onto the domain enum.

    Trivial today, but kept as the mapping seam (mirroring the other agents) so
    the workflow depends on the pure :class:`OfferVerdict`, never the agent.
    """
    return reading.verdict
