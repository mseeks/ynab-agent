"""Regression: ``_load_payee_rules`` must hydrate real ``Rule`` objects.

The gate-load path queries the durable registry over the *pydantic* data
converter (``temporal_client`` connects with it). A Temporal client-side
query decodes its payload to plain ``dict``s unless the call declares a
``result_type`` — and the gate reads ``rule.match`` (attribute access), so a
dict there raises ``AttributeError: 'dict' object has no attribute 'match'``
and the ``enrich`` activity retries forever. The unit/workflow tests never
caught this because they hand the gate real ``Rule`` objects; the
deserialization seam is only exercised through an actual registry query under
the real converter — which is exactly what this test does.
"""

from __future__ import annotations

import datetime

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import ynab_agent.workflow.temporal_client as temporal_client
from ynab_agent.domain.allocations import ProposedCategory
from ynab_agent.domain.enums import RuleSource, TrustState
from ynab_agent.domain.ids import (
    AccountId,
    CategoryId,
    RuleId,
    YnabTransactionId,
)
from ynab_agent.domain.money import Money
from ynab_agent.domain.rule import Rule, RuleAction, RuleMatch
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.learn.registry import RegistryState
from ynab_agent.policy.floor import AutoActionCounters
from ynab_agent.policy.gate import GateVerdict, evaluate_gate
from ynab_agent.workflow.activities import _load_payee_rules
from ynab_agent.workflow.registry_types import (
    REGISTRY_WORKFLOW_ID,
    RegistryParams,
)
from ynab_agent.workflow.registry_workflow import RuleRegistryWorkflow
from ynab_agent.workflow.runtime import DATA_CONVERTER

_TASK_QUEUE = "registry-load-test"


def _blessed_rule(payee: str = "Blue Bottle") -> Rule:
    """A single trusted, human-blessed rule — the kind the gate auto-applies."""
    return Rule(
        id=RuleId("r1"),
        match=RuleMatch(payee_pattern=payee),
        action=RuleAction(
            allocation=ProposedCategory(category=CategoryId("dining"))
        ),
        trust=TrustState.TRUSTED,
        source=RuleSource.HUMAN_EXPLICIT,
    )


def _snapshot(payee: str = "Blue Bottle Coffee") -> YnabSnapshot:
    return YnabSnapshot(
        ynab_id=YnabTransactionId("t1"),
        account=AccountId("a1"),
        payee=payee,
        amount=Money.from_currency("-4.50"),
        txn_date=datetime.date(2026, 5, 28),
    )


async def test_load_payee_rules_hydrates_rule_objects(
    monkeypatch: object,
) -> None:
    """The registry query returns real ``Rule``s, and the gate runs over them.

    Without ``result_type`` on the query the items come back as ``dict``s and
    ``evaluate_gate`` raises ``AttributeError`` on ``rule.match`` — the live
    enrich crash-loop. This pins the hydrated-object contract end to end.
    """
    rule = _blessed_rule()
    params = RegistryParams(state=RegistryState(rules=(rule,)))
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[RuleRegistryWorkflow],
        ),
    ):
        await env.client.start_workflow(
            RuleRegistryWorkflow.run,
            params,
            id=REGISTRY_WORKFLOW_ID,
            task_queue=_TASK_QUEUE,
        )
        # The production global the activity reads its client from.
        monkeypatch.setattr(temporal_client, "_CLIENT", env.client)  # type: ignore[attr-defined]

        loaded = await _load_payee_rules("Blue Bottle Coffee")

    # The crux: real domain objects, not dicts (the bug returned dicts here).
    assert loaded, "a matching payee rule should load"
    assert all(isinstance(r, Rule) for r in loaded)
    # And the exact production call site no longer raises — a single blessed
    # rule gates AUTO (SPEC §14).
    gate = evaluate_gate(_snapshot(), tuple(loaded), AutoActionCounters())
    assert gate.verdict is GateVerdict.AUTO
