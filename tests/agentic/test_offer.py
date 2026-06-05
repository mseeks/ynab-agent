"""Tests for the offer-reply agent and its verdict mapping (SPEC §14.7 3b)."""

from __future__ import annotations

from pydantic_ai.models.test import TestModel

from ynab_agent.agentic.offer import (
    OfferReading,
    OfferReplyRequest,
    interpret_offer,
    to_verdict,
)
from ynab_agent.domain.enums import OfferVerdict

_REQUEST = OfferReplyRequest(reply_text="sure, go ahead", payee="Spotify")


def test_to_verdict_passes_the_reading_through() -> None:
    for verdict in OfferVerdict:
        assert to_verdict(OfferReading(verdict=verdict)) is verdict


async def test_interpret_offer_reads_an_acceptance() -> None:
    model = TestModel(custom_output_args={"verdict": "accept"})
    reading = await interpret_offer(_REQUEST, model=model)
    assert to_verdict(reading) is OfferVerdict.ACCEPT


async def test_interpret_offer_reads_a_decline() -> None:
    model = TestModel(custom_output_args={"verdict": "decline"})
    reading = await interpret_offer(_REQUEST, model=model)
    assert to_verdict(reading) is OfferVerdict.DECLINE


async def test_interpret_offer_wiring_smoke_with_default_testmodel() -> None:
    # No custom output → TestModel autofills a valid verdict; proves the
    # agent/output-schema wiring round-trips.
    reading = await interpret_offer(_REQUEST, model=TestModel())
    assert isinstance(reading, OfferReading)
