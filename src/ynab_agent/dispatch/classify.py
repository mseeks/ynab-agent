"""W3 inbound classification: the deterministic routing decision (SPEC §5).

Every inbound AgentMail message is classified and routed. The parts that are
*deterministic* live here and run first — the inbound boundary (§0.6): trust the
signed webhook for provenance, act only on allow-listed senders, and never treat
an autoresponder or bounce as a confirmation. A verified, allow-listed message
on a known transaction thread routes straight to that W2; anything else without
a thread is handed to the agentic classifier (receipt vs. command).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.ids import MessageId, ThreadId, YnabTransactionId

# Local-part prefixes that mark a bounce/system message — never a real reply.
_SYSTEM_SENDERS = ("mailer-daemon@", "postmaster@")


class InboundMessage(Frozen):
    """A normalized inbound email from the AgentMail webhook.

    ``signature_verified`` is the Svix webhook provenance check, and
    ``is_auto_reply`` is derived from the headers (auto-submitted / vacation);
    both are computed by the webhook handler before classification.
    """

    message_id: MessageId
    from_address: str
    subject: str
    body: str
    thread_id: ThreadId | None = None
    signature_verified: bool = False
    is_auto_reply: bool = False


class InboundKind(StrEnum):
    """The agentic classifier's verdict for a non-thread message (SPEC §5)."""

    RECEIPT = "receipt"
    COMMAND = "command"
    NOISE = "noise"


class Quarantine(Frozen):
    """The message failed the inbound boundary; hold it, do not act."""

    kind: Literal["quarantine"] = "quarantine"
    reason: str


class Ignore(Frozen):
    """A non-actionable autoresponder/bounce; drop it silently."""

    kind: Literal["ignore"] = "ignore"
    reason: str


class RouteToTransaction(Frozen):
    """A reply on a known transaction thread; signal that W2 (SPEC §5a)."""

    kind: Literal["transaction"] = "transaction"
    txn_id: YnabTransactionId


class RouteToInterpret(Frozen):
    """No thread: hand to the agentic classifier (receipt vs. command)."""

    kind: Literal["interpret"] = "interpret"


DispatchDecision = Annotated[
    Quarantine | Ignore | RouteToTransaction | RouteToInterpret,
    Field(discriminator="kind"),
]


def _is_system_sender(address: str) -> bool:
    return address.lower().startswith(_SYSTEM_SENDERS)


def classify(
    message: InboundMessage,
    allowlist: frozenset[str],
    *,
    txn_id: YnabTransactionId | None,
) -> DispatchDecision:
    """Decide how to route an inbound message. Pure (SPEC §5, §0.6).

    Args:
        message: The normalized inbound message.
        allowlist: Lower-cased sender addresses permitted to act.
        txn_id: The transaction this thread maps to, or ``None`` if the message
            is not on a known transaction thread.

    Returns:
        The routing decision; ``RouteToInterpret`` defers receipt-vs-command to
        the agentic classifier.
    """
    if not message.signature_verified:
        return Quarantine(reason="unsigned webhook")
    if message.is_auto_reply or _is_system_sender(message.from_address):
        return Ignore(reason="autoresponder or bounce")
    if message.from_address.lower() not in allowlist:
        return Quarantine(reason="sender not allow-listed")
    if txn_id is not None:
        return RouteToTransaction(txn_id=txn_id)
    return RouteToInterpret()
