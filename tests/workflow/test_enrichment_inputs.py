"""Tests for the W2 enrichment-input mapping (SPEC §4.1).

``_load_enrichment_inputs`` reads YNAB and returns (candidates, rules, counters)
for the gate + propose step. Its one piece of pure logic is turning the budget's
category spends into the agent's candidate choices; the YNAB read, the registry
rule query, and the circuit-breaker counters read are thin glue over the durable
workflows, exercised via the workflow tests.
"""

from __future__ import annotations

from ynab_agent.budget.overspend import CategorySpend
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money
from ynab_agent.workflow.activities import _candidates_from_spends


def _spend(category: str, name: str) -> CategorySpend:
    return CategorySpend(
        category=CategoryId(category),
        name=name,
        budgeted=Money.from_currency("100"),
        activity=Money.from_currency("-40"),
        balance=Money.from_currency("60"),
    )


def test_candidates_carry_id_and_name() -> None:
    candidates = _candidates_from_spends(
        (_spend("dining", "Dining Out"), _spend("coffee", "Coffee"))
    )
    assert [(c.id, c.name) for c in candidates] == [
        ("dining", "Dining Out"),
        ("coffee", "Coffee"),
    ]


def test_candidates_empty_when_no_spends() -> None:
    assert _candidates_from_spends(()) == ()
