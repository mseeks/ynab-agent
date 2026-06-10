"""Tests for the receipt-matching agent (SPEC §6, §0.5)."""

from __future__ import annotations

import os

import pytest
from pydantic_ai.models.test import TestModel

from ynab_agent.agentic.match import (
    CandidateTxn,
    MatchDecision,
    MatchRequest,
    MatchVerdict,
    ReceiptFacts,
    match_receipt,
    to_match_outcome,
)
from ynab_agent.join.match import Ambiguous, ConfidentMatch, NoMatch

_REQUEST = MatchRequest(
    receipt=ReceiptFacts(
        merchant="Blue Bottle", total_display="$4.50", date_display="May 28"
    ),
    candidates=(
        CandidateTxn(
            id="t1",
            payee="Blue Bottle Coffee",
            amount_display="-$4.50",
            date_display="May 28",
        ),
        CandidateTxn(
            id="t2",
            payee="Whole Foods",
            amount_display="-$42.00",
            date_display="May 27",
        ),
    ),
)


def _model(**output: object) -> TestModel:
    return TestModel(custom_output_args=output)


async def test_confident_match_returns_the_chosen_txn() -> None:
    verdict = await match_receipt(
        _REQUEST, model=_model(decision="match", txn_id="t1")
    )
    assert verdict.txn_id == "t1"
    outcome = to_match_outcome(verdict, _REQUEST)
    assert isinstance(outcome, ConfidentMatch)
    assert outcome.txn_id == "t1"


def test_ambiguous_verdict_maps_to_ambiguous_outcome() -> None:
    verdict = MatchVerdict(
        decision=MatchDecision.AMBIGUOUS, candidate_ids=("t1", "t2")
    )
    outcome = to_match_outcome(verdict, _REQUEST)
    assert isinstance(outcome, Ambiguous)
    assert outcome.candidates == ("t1", "t2")


def test_no_match_verdict_maps_to_no_match() -> None:
    outcome = to_match_outcome(
        MatchVerdict(decision=MatchDecision.NO_MATCH), _REQUEST
    )
    assert isinstance(outcome, NoMatch)


def test_malformed_match_without_id_falls_back_to_no_match() -> None:
    # A "match" verdict with no txn_id violates the invariant → NoMatch.
    outcome = to_match_outcome(
        MatchVerdict(decision=MatchDecision.MATCH), _REQUEST
    )
    assert isinstance(outcome, NoMatch)


def test_ambiguous_with_one_candidate_falls_back_to_no_match() -> None:
    verdict = MatchVerdict(
        decision=MatchDecision.AMBIGUOUS, candidate_ids=("t1",)
    )
    assert isinstance(to_match_outcome(verdict, _REQUEST), NoMatch)


@pytest.mark.skipif(
    not os.environ.get("YNAB_AGENT_LIVE_OLLAMA"),
    reason="set YNAB_AGENT_LIVE_OLLAMA=1 to run the live Gemma smoke",
)
async def test_live_gemma_matches_the_obvious_receipt() -> None:
    # SPEC §0.5 spike #2: real Gemma should pick the exact-amount match.
    verdict = await match_receipt(_REQUEST)
    assert verdict.decision.value in {"match", "ambiguous", "no_match"}


def test_match_to_a_hallucinated_id_falls_back_to_no_match() -> None:
    # The prompt demands ids from the candidates, but a write-adjacent
    # decision never rests on a prompt: an invented id parks, never attaches.
    verdict = MatchVerdict(decision=MatchDecision.MATCH, txn_id="invented")
    assert isinstance(to_match_outcome(verdict, _REQUEST), NoMatch)


def test_ambiguous_ids_are_filtered_to_real_candidates() -> None:
    # One invented id among the candidates drops out; if fewer than two valid
    # ids remain, the whole verdict collapses to NoMatch.
    mixed = MatchVerdict(
        decision=MatchDecision.AMBIGUOUS,
        candidate_ids=("t1", "invented", "t2", "t1"),
    )
    outcome = to_match_outcome(mixed, _REQUEST)
    assert isinstance(outcome, Ambiguous)
    assert outcome.candidates == ("t1", "t2")  # deduped, order kept
    thin = MatchVerdict(
        decision=MatchDecision.AMBIGUOUS, candidate_ids=("t1", "invented")
    )
    assert isinstance(to_match_outcome(thin, _REQUEST), NoMatch)
