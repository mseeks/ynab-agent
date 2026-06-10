"""The receipt-parsing agent: a forwarded email into structured facts (§6).

The data-extraction half of W4. Given a forwarded email's subject and body,
the model extracts the receipt's facts — merchant, total, date, line items,
split hints — as strings; :func:`to_receipt` then converts them
*deterministically* into the domain :class:`~ynab_agent.domain.receipt.Receipt`
(exact ``Money``, a real ``date``), dropping anything that does not parse.
The model only ever extracts; it never invents, decides, or matches — and a
message it cannot read as a receipt comes back ``is_receipt=False``, which the
caller answers with an honest "couldn't read this" note rather than parking
junk.

The model is injected per run so tests drive a ``TestModel`` offline;
production uses :func:`~ynab_agent.agentic.model.build_model` (Ollama).
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from pydantic_ai import Agent

from ynab_agent.agentic.model import run_structured
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.money import Money
from ynab_agent.domain.receipt import Receipt, ReceiptLineItem

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from ynab_agent.domain.ids import MessageId, ReceiptId, ThreadId

# Bound the model's reading: receipts say what they say early; HTML-heavy
# forwards can run long. Items are capped so a grocery run never floods the
# memo machinery downstream.
MAX_BODY_CHARS = 4000
_MAX_ITEMS = 12

_AMOUNT = re.compile(r"-?\d{1,7}(?:[.,]\d{1,2})?")


class ReceiptParseRequest(Frozen):
    """The forwarded email the agent extracts from."""

    subject: str
    body: str


class ParsedLineItem(Frozen):
    """One extracted line item (amounts as written, e.g. ``"$4.99"``)."""

    description: str
    amount: str | None = None


class ParsedReceipt(Frozen):
    """The agent's extraction (strings; ``to_receipt`` makes them exact).

    ``is_receipt`` is the honesty bit: a newsletter, a shipping notice with no
    charge, or a plain note must come back ``False`` rather than as invented
    facts.
    """

    is_receipt: bool
    merchant: str | None = None
    total: str | None = None
    date: str | None = None  # ISO format (YYYY-MM-DD) when present
    line_items: tuple[ParsedLineItem, ...] = ()
    split_notes: str | None = None


_SYSTEM_PROMPT = """\
You extract purchase facts from one forwarded email. You are given the subject
and body text.

If the email is a purchase receipt, an order confirmation, or an invoice for a
completed charge, set `is_receipt` true and extract ONLY what is written:
  - `merchant`: the business charged (e.g. "Whole Foods", "Amazon").
  - `total`: the final amount CHARGED, as written (e.g. "$23.48"). Prefer the
    grand total (after tax/tip/shipping) over subtotals.
  - `date`: the purchase date in ISO format (YYYY-MM-DD), only if stated.
  - `line_items`: the purchased items (short descriptions, with each item's
    price as written when shown).
  - `split_notes`: any note the FORWARDER added about splitting or who it was
    for (e.g. "the $40 part is mine") — null when none.

Anything else — a newsletter, shipping update with no charge, refund,
marketing, or a plain message — set `is_receipt` false and leave every other
field null. Never invent a value that is not in the text; a missing field
stays null."""

_AGENT: Agent[None, ParsedReceipt] = Agent(
    output_type=ParsedReceipt,
    system_prompt=_SYSTEM_PROMPT,
)


def _format_request(request: ReceiptParseRequest) -> str:
    """Render the request as the agent's user prompt (body bounded)."""
    return (
        f"Subject: {request.subject}\n\nBody:\n{request.body[:MAX_BODY_CHARS]}"
    )


async def parse_receipt(
    request: ReceiptParseRequest, *, model: Model | None = None
) -> ParsedReceipt:
    """Run the receipt-extraction agent for one forwarded email (SPEC §6).

    Args:
        request: The forwarded email's subject and body.
        model: A model to use; defaults to the configured Ollama/Gemma.

    Returns:
        The agent's structured extraction.
    """
    return await run_structured(
        _AGENT,
        _format_request(request),
        output_type=ParsedReceipt,
        model=model,
    )


def _money(raw: str | None) -> Money | None:
    """An exact, positive ``Money`` from an as-written amount, or ``None``.

    ``"$1,234.56"`` → 1234.56; a receipt total is a magnitude, so the sign is
    dropped (YNAB outflows are negative; matching compares magnitudes).
    Anything that does not contain a parseable number is ``None`` — never a
    guess.
    """
    if not raw:
        return None
    match = _AMOUNT.search(raw.replace(",", ""))
    if match is None:
        return None
    try:
        value = abs(Decimal(match.group().replace(",", ".")))
    except InvalidOperation:  # pragma: no cover - regex precludes this
        return None
    if value == 0:
        return None
    return Money.from_currency(value)


def _date(raw: str | None) -> datetime.date | None:
    """A real date from the model's ISO string, or ``None``."""
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def to_receipt(
    parsed: ParsedReceipt,
    *,
    receipt_id: ReceiptId,
    now: datetime.datetime,
    message_id: MessageId,
    thread_id: ThreadId | None,
) -> Receipt | None:
    """Convert an extraction into a domain Receipt, or ``None`` (SPEC §6).

    Deterministic: money and dates are parsed exactly, junk fields drop to
    ``None``, and items are capped. A non-receipt — or one with neither a
    parseable total nor a merchant — returns ``None``: there is nothing a
    matcher could responsibly do with it, so the caller answers honestly
    instead of parking it.
    """
    if not parsed.is_receipt:
        return None
    total = _money(parsed.total)
    merchant = (parsed.merchant or "").strip() or None
    if total is None and merchant is None:
        return None
    items = tuple(
        ReceiptLineItem(
            description=item.description.strip(), amount=_money(item.amount)
        )
        for item in parsed.line_items[:_MAX_ITEMS]
        if item.description.strip()
    )
    split = (parsed.split_notes or "").strip() or None
    return Receipt(
        id=receipt_id,
        parked_at=now,
        merchant=merchant,
        date=_date(parsed.date),
        total=total,
        line_items=items,
        split_notes=split,
        source_message_id=message_id,
        source_thread_id=thread_id,
    )
