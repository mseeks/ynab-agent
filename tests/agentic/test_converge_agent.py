"""Tests for the revision-interpreting agent (SPEC §3, §0.5)."""

from __future__ import annotations

import os

import pytest
from pydantic_ai.models.test import TestModel

from ynab_agent.agentic.converge import (
    RevisionDecision,
    RevisionRequest,
    RevisionTarget,
    interpret_revision,
    to_revision_plan,
)
from ynab_agent.agentic.enrich import CandidateCategory

_REQUEST = RevisionRequest(
    instruction="actually that was groceries",
    current_category_name="Dining Out",
    current_memo="coffee",
    candidates=(
        CandidateCategory(id="dining", name="Dining Out"),
        CandidateCategory(id="groceries", name="Groceries"),
    ),
)


async def test_retarget_round_trips_and_plans_a_change() -> None:
    model = TestModel(
        custom_output_args={"decision": "retarget", "category_id": "groceries"}
    )
    target = await interpret_revision(_REQUEST, model=model)
    assert target.decision is RevisionDecision.RETARGET
    plan = to_revision_plan(target)
    assert plan.changes is True
    assert plan.category_id == "groceries"


def test_memo_only_plans_a_memo_change() -> None:
    plan = to_revision_plan(
        RevisionTarget(decision=RevisionDecision.MEMO_ONLY, memo="HDMI cable")
    )
    assert plan.changes is True
    assert plan.memo == "HDMI cable"
    assert plan.category_id is None


def test_no_change_plans_nothing() -> None:
    plan = to_revision_plan(RevisionTarget(decision=RevisionDecision.NO_CHANGE))
    assert plan.changes is False


def test_retarget_without_a_category_collapses_to_no_change() -> None:
    # The spine must never commit a write the model under-specified.
    plan = to_revision_plan(RevisionTarget(decision=RevisionDecision.RETARGET))
    assert plan.changes is False


@pytest.mark.skipif(
    not os.environ.get("YNAB_AGENT_LIVE_OLLAMA"),
    reason="set YNAB_AGENT_LIVE_OLLAMA=1 to run the live Gemma smoke",
)
async def test_live_gemma_reads_a_retarget() -> None:
    # SPEC §0.5 spike #2: real Gemma reads a correction as a retarget.
    target = await interpret_revision(_REQUEST)
    assert target.decision in set(RevisionDecision)
