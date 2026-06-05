"""W3 inbound classification: the deterministic routing decision (SPEC §5).

Every inbound AgentMail message is classified and routed. The parts that are
*deterministic* live here and run first — the inbound boundary (§0.6): trust the
signed webhook for provenance, act only on allow-listed senders, and never treat
an autoresponder or bounce as a confirmation. A verified, allow-listed message
on a known transaction thread routes straight to that W2; anything else without
a thread is handed to the agentic classifier (receipt vs. command).
"""

from __future__ import annotations

from email.utils import parseaddr
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


class RouteToOffer(Frozen):
    """A reply on an autonomy-offer thread; signal that offer workflow (3b)."""

    kind: Literal["offer"] = "offer"
    offer_id: str


class RouteToInterpret(Frozen):
    """No thread: hand to the agentic classifier (receipt vs. command)."""

    kind: Literal["interpret"] = "interpret"


DispatchDecision = Annotated[
    Quarantine | Ignore | RouteToTransaction | RouteToOffer | RouteToInterpret,
    Field(discriminator="kind"),
]


def _sender_address(raw: str) -> str:
    """The bare, lower-cased email from a possibly display-named ``From``.

    AgentMail surfaces the raw ``From`` header, which a mail client may send as
    ``"Real Name <addr@host>"`` rather than a bare address. The owner allow-list
    holds bare addresses, so the display-name form must be reduced to its
    address before the membership (and system-sender) checks — otherwise a
    legitimate owner reply is quarantined as "sender not allow-listed".
    ``parseaddr`` returns the address unchanged when there is no display name;
    on a parse miss we fall back to the raw value so the allow-list never
    silently widens.
    """
    return (parseaddr(raw)[1] or raw).lower()


def classify(
    message: InboundMessage,
    allowlist: frozenset[str],
    *,
    txn_id: YnabTransactionId | None,
    offer_id: str | None = None,
) -> DispatchDecision:
    """Decide how to route an inbound message. Pure (SPEC §5, §0.6).

    Args:
        message: The normalized inbound message.
        allowlist: Lower-cased sender addresses permitted to act.
        txn_id: The transaction this thread maps to, or ``None`` if the message
            is not on a known transaction thread.
        offer_id: The autonomy-offer workflow this thread maps to, or ``None``.
            A transaction thread takes precedence (a message is on at most one).

    Returns:
        The routing decision; ``RouteToInterpret`` defers receipt-vs-command to
        the agentic classifier.
    """
    if not message.signature_verified:
        return Quarantine(reason="unsigned webhook")
    sender = _sender_address(message.from_address)
    if message.is_auto_reply or sender.startswith(_SYSTEM_SENDERS):
        return Ignore(reason="autoresponder or bounce")
    if sender not in allowlist:
        return Quarantine(reason="sender not allow-listed")
    if txn_id is not None:
        return RouteToTransaction(txn_id=txn_id)
    if offer_id is not None:
        return RouteToOffer(offer_id=offer_id)
    return RouteToInterpret()
