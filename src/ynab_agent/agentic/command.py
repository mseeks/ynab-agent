"""The command-parsing agent: standing instructions (SPEC §5c, §14.2).

The agentic half of W3's ``handle_command``. An owner can grant autonomy
directly — "always categorize Spotify as Subscriptions", "auto-handle Costco as
Groceries" — instead of waiting for a rule to earn eligibility. The model reads
such a message into a :class:`CommandReading`: whether it is a *bless*
(and for which payee + category), or something else the agent does not act on in
v1. The deterministic :func:`to_explicit_command` then turns a bless into the
:class:`~ynab_agent.learn.events.ExplicitCommand` the registry folds — so the
model only classifies and picks a category, never mints the trust grant itself.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field
from pydantic_ai import Agent

from ynab_agent.agentic.enrich import CandidateCategory
from ynab_agent.agentic.model import run_structured
from ynab_agent.domain.allocations import ProposedCategory
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.rule import RuleAction, RuleMatch
from ynab_agent.learn.events import ExplicitCommand

if TYPE_CHECKING:
    from pydantic_ai.models import Model


class CommandKind(StrEnum):
    """What a standing command asked for."""

    BLESS = "bless"
    OTHER = "other"


class CommandRequest(Frozen):
    """A command message and the categories it may name."""

    command_text: str
    candidates: tuple[CandidateCategory, ...] = Field(min_length=1)


class CommandReading(Frozen):
    """The agent's read of a command (mapped to an ExplicitCommand or dropped).

    ``payee_pattern`` is the substring future transactions match on (the
    merchant the owner named); ``category_id`` is the chosen candidate. Both are
    required for a ``bless`` to take effect.
    """

    kind: CommandKind
    payee_pattern: str | None = None
    category_id: str | None = None


_SYSTEM_PROMPT = """\
You read one standing-instruction message an account owner sent to a budgeting
agent. You are given the message text and the candidate categories (id + name).

Decide if it is a `bless` — a request to ALWAYS / automatically categorize a
named merchant a certain way from now on (e.g. "always categorize Spotify as
Subscriptions", "auto-handle Costco as Groceries", "you can always file Netflix
under TV"). If so:
  - set `payee_pattern` to the merchant name to match (a short substring, as it
    appears in transactions — e.g. "Spotify", "Costco");
  - set `category_id` to the matching candidate id.
Otherwise (a question, a one-off correction, a comment, anything you are unsure
about) return `other` with no fields. When in doubt, prefer `other`: granting
standing autonomy is consequential, so only do it on a clear, explicit request
that also names a category you can match to a candidate."""

_AGENT: Agent[None, CommandReading] = Agent(
    output_type=CommandReading,
    system_prompt=_SYSTEM_PROMPT,
)


def _format_request(request: CommandRequest) -> str:
    """Render the request as the agent's user prompt."""
    lines = [f"Message: {request.command_text}", "Candidate categories:"]
    lines.extend(f"  - {c.name} (id: {c.id})" for c in request.candidates)
    return "\n".join(lines)


async def parse_command(
    request: CommandRequest, *, model: Model | None = None
) -> CommandReading:
    """Run the command-parsing agent for one message (SPEC §5c)."""
    return await run_structured(
        _AGENT,
        _format_request(request),
        output_type=CommandReading,
        model=model,
    )


def to_explicit_command(
    reading: CommandReading, candidates: tuple[CandidateCategory, ...]
) -> ExplicitCommand | None:
    """Turn a ``bless`` reading into an ExplicitCommand, or ``None`` (SPEC §14).

    Declines anything that is not a clear bless with both a payee and a
    *resolved* category — the id must be a real candidate, since a standing
    rule against a hallucinated id would auto-file every future match into a
    category that does not exist. The conservative move, always: blessing
    grants standing autonomy.
    """
    if reading.kind is not CommandKind.BLESS:
        return None
    if not reading.payee_pattern or not reading.category_id:
        return None
    if not any(c.id == reading.category_id for c in candidates):
        return None
    return ExplicitCommand(
        match=RuleMatch(payee_pattern=reading.payee_pattern),
        action=RuleAction(
            allocation=ProposedCategory(
                category=CategoryId(reading.category_id)
            )
        ),
    )
