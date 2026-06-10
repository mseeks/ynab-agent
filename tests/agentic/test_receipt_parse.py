"""Tests for the receipt-extraction agent and its domain conversion (§6)."""

from __future__ import annotations

import datetime

from pydantic_ai.models.test import TestModel

from ynab_agent.agentic.receipt_parse import (
    MAX_BODY_CHARS,
    ParsedLineItem,
    ParsedReceipt,
    ReceiptParseRequest,
    _format_request,
    parse_receipt,
    to_receipt,
)
from ynab_agent.domain.ids import MessageId, ReceiptId, ThreadId
from ynab_agent.domain.money import Money
from ynab_agent.domain.receipt import Receipt, receipt_summary

_NOW = datetime.datetime(2026, 6, 10, 12, 0, tzinfo=datetime.UTC)


def _build(parsed: ParsedReceipt) -> Receipt | None:
    """to_receipt with the fixed test identifiers."""
    return to_receipt(
        parsed,
        receipt_id=ReceiptId("r1"),
        now=_NOW,
        message_id=MessageId("m1"),
        thread_id=ThreadId("thr1"),
    )


async def test_parse_round_trips_through_the_agent() -> None:
    model = TestModel(
        custom_output_args={
            "is_receipt": True,
            "merchant": "Whole Foods",
            "total": "$23.48",
            "date": "2026-06-08",
            "line_items": [
                {"description": "Corn Starch", "amount": "$2.29"},
            ],
        }
    )
    out = await parse_receipt(
        ReceiptParseRequest(subject="Your receipt", body="..."), model=model
    )
    assert out.is_receipt is True
    assert out.merchant == "Whole Foods"
    assert out.total == "$23.48"


def test_body_is_bounded_in_the_prompt() -> None:
    request = ReceiptParseRequest(subject="s", body="x" * (MAX_BODY_CHARS * 2))
    assert len(_format_request(request)) < MAX_BODY_CHARS + 100


def test_to_receipt_converts_money_and_date_exactly() -> None:
    parsed = ParsedReceipt(
        is_receipt=True,
        merchant="  Whole Foods  ",
        total="$1,234.56",
        date="2026-06-08",
        line_items=(
            ParsedLineItem(description="Corn Starch", amount="$2.29"),
            ParsedLineItem(description="  "),  # blank items drop
        ),
        split_notes="  ",
    )
    receipt = _build(parsed)
    assert receipt is not None
    assert receipt.merchant == "Whole Foods"
    assert receipt.total == Money.from_currency("1234.56")
    assert receipt.date == datetime.date(2026, 6, 8)
    assert len(receipt.line_items) == 1
    assert receipt.line_items[0].amount == Money.from_currency("2.29")
    assert receipt.split_notes is None  # blank → None, never ""
    assert receipt.source_thread_id == "thr1"
    assert receipt.parked_at == _NOW


def test_to_receipt_total_sign_is_a_magnitude() -> None:
    parsed = ParsedReceipt(is_receipt=True, merchant="X", total="-$5.00")
    receipt = _build(parsed)
    assert receipt is not None
    assert receipt.total == Money.from_currency("5.00")


def test_non_receipt_returns_none() -> None:
    assert _build(ParsedReceipt(is_receipt=False)) is None


def test_receipt_with_no_substance_returns_none() -> None:
    # Neither a parseable total nor a merchant: nothing to match on.
    parsed = ParsedReceipt(is_receipt=True, total="around forty", merchant=" ")
    assert _build(parsed) is None


def test_junk_date_drops_to_none_but_the_receipt_survives() -> None:
    parsed = ParsedReceipt(
        is_receipt=True, merchant="Costco", total="$80", date="last tuesday"
    )
    receipt = _build(parsed)
    assert receipt is not None
    assert receipt.date is None


def test_items_are_capped() -> None:
    parsed = ParsedReceipt(
        is_receipt=True,
        merchant="Amazon",
        total="$99",
        line_items=tuple(
            ParsedLineItem(description=f"item {i}") for i in range(30)
        ),
    )
    receipt = _build(parsed)
    assert receipt is not None
    assert len(receipt.line_items) == 12


def test_summary_reads_like_a_human_line() -> None:
    parsed = ParsedReceipt(
        is_receipt=True,
        merchant="Whole Foods",
        total="$23.48",
        line_items=(
            ParsedLineItem(description="Corn Starch"),
            ParsedLineItem(description="Paper Towels"),
            ParsedLineItem(description="Milk"),
            ParsedLineItem(description="Eggs"),
        ),
    )
    receipt = _build(parsed)
    assert receipt is not None
    summary = receipt_summary(receipt)
    assert "Whole Foods — $23.48" in summary
    assert "Corn Starch, Paper Towels, Milk (+1 more)" in summary
