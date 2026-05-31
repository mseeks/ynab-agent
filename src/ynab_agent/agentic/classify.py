"""The inbound-classifier agent: receipt, command, or noise? (SPEC §5).

The agentic half of W3's ``classify_inbound``. The deterministic dispatcher
(``dispatch.classify``) has already verified provenance and the allow-list and
found no transaction thread; this agent reads an un-threaded message from a
trusted sender and decides what it is — a forwarded receipt (→ W4), an ad-hoc
command (e.g. "always categorize Costco as Groceries"), or noise. The verdict is
advisory routing: the deterministic spine still applies the floor and asks for
confirmation on any standing rule before acting on it (SPEC §0.6).

:func:`to_kind` projects the agent's verdict onto the domain ``InboundKind``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import Agent

from ynab_agent.agentic.model import build_model
from ynab_agent.dispatch.classify import InboundKind
from ynab_agent.domain.base import Frozen

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from ynab_agent.dispatch.classify import InboundMessage


class InboundClassification(Frozen):
    """The agent's structured verdict for an un-threaded message."""

    kind: InboundKind
    reason: str


_SYSTEM_PROMPT = """\
You triage one inbound email from a trusted household member. It is NOT a reply
on an existing transaction thread. Decide what it is:

- `receipt`: a forwarded purchase receipt or order confirmation (a merchant, an
  amount, line items) — it will be matched to a transaction.
- `command`: an explicit instruction to the budgeting agent, e.g. "always
  categorize Costco as Groceries", "split Costco 50/50", "never auto-approve
  Amazon".
- `noise`: anything else — chit-chat, newsletters, unrelated mail.

Give a one-sentence reason. When genuinely unsure between receipt and command,
pick the better fit; when it is clearly neither, choose `noise`."""

_AGENT: Agent[None, InboundClassification] = Agent(
    output_type=InboundClassification,
    system_prompt=_SYSTEM_PROMPT,
)


def _format_message(message: InboundMessage) -> str:
    """Render the message as the agent's user prompt."""
    return "\n".join(
        [
            f"From: {message.from_address}",
            f"Subject: {message.subject}",
            "Body:",
            message.body,
        ]
    )


async def classify_inbound(
    message: InboundMessage, *, model: Model | None = None
) -> InboundClassification:
    """Run the inbound-classifier agent for one message (SPEC §5).

    Args:
        message: The un-threaded, allow-listed inbound message.
        model: A model to use; defaults to the configured Ollama/Gemma.

    Returns:
        The agent's structured classification.
    """
    run_model = model if model is not None else build_model()
    result = await _AGENT.run(_format_message(message), model=run_model)
    return result.output


def to_kind(classification: InboundClassification) -> InboundKind:
    """Project the agent's verdict onto the domain InboundKind (SPEC §5)."""
    return classification.kind
