"""Tests for the budget-balancer agent: propose, reply, and mappings (§8).

Offline tests drive a ``TestModel`` (no network, deterministic). The default
``TestModel`` smoke tests prove the agent/output-schema wiring round-trips *with
the calculator tools registered* — TestModel calls each tool once before
producing output, so a broken tool signature would surface here.
"""

from __future__ import annotations

from pydantic_ai.models.test import TestModel

from ynab_agent.agentic.balance import (
    BalanceContext,
    BalanceProposal,
    BalanceReading,
    BalanceReplyRequest,
    BalanceVerdict,
    MoveSpec,
    OfferedOption,
    ProposedOption,
    SourceFunds,
    add,
    interpret_balance_reply,
    multiply,
    propose_balance,
    subtract,
    sum_amounts,
    to_balance_outcome,
    to_options,
)
from ynab_agent.budget.balance import ApplyMoves, ClarifyBalance, DeclineBalance
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money

_CONTEXT = BalanceContext(
    needy_category_id="dining",
    needy_category_name="Dining Out",
    shortfall=120.0,
    overspend_note="Dining is $420 of $400, trending to ~$520.",
    sources=(
        SourceFunds(
            id="rta",
            name="Ready to Assign",
            available=100.0,
            kind="ready-to-assign",
        ),
        SourceFunds(
            id="buffer", name="Buffer", available=500.0, kind="category"
        ),
    ),
)

_REPLY = BalanceReplyRequest(
    reply_text="do option 1",
    needy_category_name="Dining Out",
    shortfall=120.0,
    options=(
        OfferedOption(
            label="From Buffer",
            moves=(MoveSpec(source_category_id="buffer", amount=120.0),),
            rationale="Buffer has plenty.",
        ),
    ),
    sources=_CONTEXT.sources,
)


# --- The calculator tool functions. -------------------------------------------


def test_calculator_tools_compute() -> None:
    assert add(70.0, 50.0) == 120.0
    assert subtract(120.0, 50.0) == 70.0
    assert multiply(120.0, 0.5) == 60.0
    assert sum_amounts([70.0, 50.0, 20.0]) == 140.0


# --- to_options: model proposal -> domain options. ----------------------------


def test_to_options_injects_destination_and_exact_money() -> None:
    proposal = BalanceProposal(
        options=(
            ProposedOption(
                label="Split",
                moves=(
                    MoveSpec(source_category_id="rta", amount=70.0),
                    MoveSpec(source_category_id="buffer", amount=50.0),
                ),
                rationale="Empty RTA first, top up from buffer.",
            ),
        )
    )
    options = to_options(proposal, destination=CategoryId("dining"))
    assert len(options) == 1
    move = options[0].moves[0]
    assert move.source == "rta"
    assert move.destination == "dining"  # injected, not from the model
    assert move.amount == Money.from_currency("70")
    assert options[0].total == Money.from_currency("120")


# --- to_balance_outcome: reply reading -> domain outcome. ---------------------


def test_to_balance_outcome_apply_builds_moves() -> None:
    reading = BalanceReading(
        verdict=BalanceVerdict.APPLY,
        moves=(MoveSpec(source_category_id="buffer", amount=120.0),),
    )
    outcome = to_balance_outcome(reading, destination=CategoryId("dining"))
    assert isinstance(outcome, ApplyMoves)
    assert outcome.moves[0].source == "buffer"
    assert outcome.moves[0].destination == "dining"
    assert outcome.moves[0].amount == Money.from_currency("120")


def test_to_balance_outcome_apply_without_moves_clarifies() -> None:
    reading = BalanceReading(verdict=BalanceVerdict.APPLY, moves=())
    outcome = to_balance_outcome(reading, destination=CategoryId("dining"))
    assert isinstance(outcome, ClarifyBalance)


def test_to_balance_outcome_decline() -> None:
    reading = BalanceReading(verdict=BalanceVerdict.DECLINE)
    outcome = to_balance_outcome(reading, destination=CategoryId("dining"))
    assert isinstance(outcome, DeclineBalance)


def test_to_balance_outcome_unclear_carries_the_question() -> None:
    reading = BalanceReading(
        verdict=BalanceVerdict.UNCLEAR, question="From which category?"
    )
    outcome = to_balance_outcome(reading, destination=CategoryId("dining"))
    assert isinstance(outcome, ClarifyBalance)
    assert outcome.question == "From which category?"


# --- The agents, offline. -----------------------------------------------------


async def test_propose_balance_returns_structured_options() -> None:
    model = TestModel(
        custom_output_args={
            "options": [
                {
                    "label": "From Buffer",
                    "moves": [
                        {"source_category_id": "buffer", "amount": 120.0}
                    ],
                    "rationale": "Buffer has room.",
                }
            ]
        }
    )
    proposal = await propose_balance(_CONTEXT, model=model)
    options = to_options(proposal, destination=CategoryId("dining"))
    assert options[0].moves[0].source == "buffer"
    assert options[0].total == Money.from_currency("120")


async def test_propose_balance_wiring_smoke_with_calculator_tools() -> None:
    # No custom output: TestModel calls each registered calculator tool once
    # before autofilling a valid proposal, so this proves the tools + output
    # schema all wire together offline.
    proposal = await propose_balance(_CONTEXT, model=TestModel())
    assert isinstance(proposal, BalanceProposal)


async def test_interpret_reply_reads_an_apply() -> None:
    model = TestModel(
        custom_output_args={
            "verdict": "apply",
            "moves": [{"source_category_id": "buffer", "amount": 120.0}],
            "question": None,
        }
    )
    reading = await interpret_balance_reply(_REPLY, model=model)
    outcome = to_balance_outcome(reading, destination=CategoryId("dining"))
    assert isinstance(outcome, ApplyMoves)


async def test_interpret_reply_reads_a_decline() -> None:
    model = TestModel(
        custom_output_args={"verdict": "decline", "moves": [], "question": None}
    )
    reading = await interpret_balance_reply(_REPLY, model=model)
    assert (
        to_balance_outcome(reading, destination=CategoryId("dining"))
        == DeclineBalance()
    )


async def test_interpret_reply_wiring_smoke_with_calculator_tools() -> None:
    reading = await interpret_balance_reply(_REPLY, model=TestModel())
    assert isinstance(reading, BalanceReading)
