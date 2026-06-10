"""Tests for the command-parsing agent and its ExplicitCommand mapping (§14)."""

from __future__ import annotations

from pydantic_ai.models.test import TestModel

from ynab_agent.agentic.command import (
    CommandKind,
    CommandReading,
    CommandRequest,
    parse_command,
    to_explicit_command,
)
from ynab_agent.agentic.enrich import CandidateCategory
from ynab_agent.domain.allocations import ProposedCategory

_CANDIDATES = (
    CandidateCategory(id="subscriptions", name="Subscriptions"),
    CandidateCategory(id="groceries", name="Groceries"),
)


def test_bless_reading_maps_to_an_explicit_command() -> None:
    command = to_explicit_command(
        CommandReading(
            kind=CommandKind.BLESS,
            payee_pattern="Spotify",
            category_id="subscriptions",
        ),
        _CANDIDATES,
    )
    assert command is not None
    assert command.match.payee_pattern == "Spotify"
    assert isinstance(command.action.allocation, ProposedCategory)
    assert command.action.allocation.category == "subscriptions"


def test_other_reading_is_declined() -> None:
    assert (
        to_explicit_command(CommandReading(kind=CommandKind.OTHER), _CANDIDATES)
        is None
    )


def test_bless_without_a_category_is_declined() -> None:
    # A bless that names no category cannot grant autonomy — drop it.
    assert (
        to_explicit_command(
            CommandReading(kind=CommandKind.BLESS, payee_pattern="Spotify"),
            _CANDIDATES,
        )
        is None
    )


def test_bless_with_a_hallucinated_category_is_declined() -> None:
    # A standing rule against an invented id would auto-file every future
    # match into a category that does not exist — decline the grant.
    assert (
        to_explicit_command(
            CommandReading(
                kind=CommandKind.BLESS,
                payee_pattern="Spotify",
                category_id="10683d916894",
            ),
            _CANDIDATES,
        )
        is None
    )


async def test_parse_command_round_trips_through_the_agent() -> None:
    model = TestModel(
        custom_output_args={
            "kind": "bless",
            "payee_pattern": "Spotify",
            "category_id": "subscriptions",
        }
    )
    reading = await parse_command(
        CommandRequest(
            command_text="always file Spotify as Subscriptions",
            candidates=_CANDIDATES,
        ),
        model=model,
    )
    assert reading.kind is CommandKind.BLESS
    assert to_explicit_command(reading, _CANDIDATES) is not None
