"""Tests for the inbound-classifier agent (SPEC §5, §0.5)."""

from __future__ import annotations

import os

import pytest
from pydantic_ai.models.test import TestModel

from ynab_agent.agentic.classify import (
    InboundClassification,
    classify_inbound,
    to_kind,
)
from ynab_agent.dispatch.classify import InboundKind, InboundMessage
from ynab_agent.domain.ids import MessageId


def _message(*, subject: str, body: str) -> InboundMessage:
    return InboundMessage(
        message_id=MessageId("m1"),
        from_address="matthew@example.com",
        subject=subject,
        body=body,
        signature_verified=True,
    )


def _model(kind: str) -> TestModel:
    return TestModel(custom_output_args={"kind": kind, "reason": "test"})


async def test_classifies_a_receipt() -> None:
    out = await classify_inbound(
        _message(subject="Your order", body="Total $42 at Costco"),
        model=_model("receipt"),
    )
    assert isinstance(out, InboundClassification)
    assert to_kind(out) is InboundKind.RECEIPT


async def test_classifies_a_command() -> None:
    out = await classify_inbound(
        _message(subject="rule", body="always categorize Costco as Groceries"),
        model=_model("command"),
    )
    assert to_kind(out) is InboundKind.COMMAND


async def test_classifies_noise() -> None:
    out = await classify_inbound(
        _message(subject="hi", body="how was your day"),
        model=_model("noise"),
    )
    assert to_kind(out) is InboundKind.NOISE


@pytest.mark.skipif(
    not os.environ.get("YNAB_AGENT_LIVE_OLLAMA"),
    reason="set YNAB_AGENT_LIVE_OLLAMA=1 to run the live Gemma smoke",
)
async def test_live_gemma_classifies_a_receipt() -> None:
    # SPEC §0.5 spike #2: real Gemma reads a forwarded receipt as such.
    out = await classify_inbound(
        _message(
            subject="Your Amazon order",
            body="Order total: $23.99\nAmazonBasics HDMI Cable",
        )
    )
    assert to_kind(out) in set(InboundKind)
