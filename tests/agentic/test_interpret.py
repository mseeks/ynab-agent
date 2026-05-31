"""Tests for the reply-interpreting agent (SPEC §3, §0.5)."""

from __future__ import annotations

import datetime
import os

import pytest
from pydantic_ai.models.test import TestModel

from ynab_agent.agentic.enrich import CandidateCategory
from ynab_agent.agentic.interpret import (
    Interpretation,
    InterpretRequest,
    ReplyIntent,
    interpret,
    to_reply_outcome,
)
from ynab_agent.domain.allocations import ResolvedCategory
from ynab_agent.domain.enums import DecidedBy
from ynab_agent.domain.ids import CategoryId
from ynab_agent.workflow.types import (
    AnswerOutcome,
    ClarifyOutcome,
    ReplyOutcome,
)

_NOW = datetime.datetime(2026, 5, 31, 12, 0, tzinfo=datetime.UTC)
_PROPOSED = CategoryId("dining")
_REQUEST = InterpretRequest(
    reply_text="ok",
    payee="Blue Bottle Coffee",
    amount_display="-$4.50",
    proposed_category_name="Dining Out",
    candidates=(
        CandidateCategory(id="dining", name="Dining Out"),
        CandidateCategory(id="coffee", name="Coffee Shops"),
    ),
)


def _outcome(interpretation: Interpretation) -> ReplyOutcome:
    return to_reply_outcome(
        interpretation, proposed_category=_PROPOSED, decided_at=_NOW
    )


async def test_approve_intent_round_trips_through_the_agent() -> None:
    model = TestModel(custom_output_args={"intent": "approve"})
    out = await interpret(_REQUEST, model=model)
    assert out.intent is ReplyIntent.APPROVE


def test_approve_commits_the_proposed_category() -> None:
    outcome = _outcome(Interpretation(intent=ReplyIntent.APPROVE))
    assert isinstance(outcome, AnswerOutcome)
    assert isinstance(outcome.decision.allocation, ResolvedCategory)
    assert outcome.decision.allocation.category == "dining"
    assert outcome.decision.decided_by is DecidedBy.HUMAN
    assert outcome.decision.decided_at == _NOW


def test_recategorize_commits_the_named_category() -> None:
    outcome = _outcome(
        Interpretation(intent=ReplyIntent.RECATEGORIZE, category_id="coffee")
    )
    assert isinstance(outcome, AnswerOutcome)
    assert isinstance(outcome.decision.allocation, ResolvedCategory)
    assert outcome.decision.allocation.category == "coffee"


def test_recategorize_without_a_category_asks_instead() -> None:
    outcome = _outcome(Interpretation(intent=ReplyIntent.RECATEGORIZE))
    assert isinstance(outcome, ClarifyOutcome)


def test_clarify_sends_the_question_back() -> None:
    outcome = _outcome(
        Interpretation(intent=ReplyIntent.CLARIFY, question="Split it how?")
    )
    assert isinstance(outcome, ClarifyOutcome)
    assert outcome.question == "Split it how?"


@pytest.mark.skipif(
    not os.environ.get("YNAB_AGENT_LIVE_OLLAMA"),
    reason="set YNAB_AGENT_LIVE_OLLAMA=1 to run the live Gemma smoke",
)
async def test_live_gemma_reads_an_approval() -> None:
    # SPEC §0.5 spike #2: real Gemma reads a plain "ok" as some valid intent.
    out = await interpret(_REQUEST)
    assert out.intent in set(ReplyIntent)
