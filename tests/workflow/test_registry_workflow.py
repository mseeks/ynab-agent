"""End-to-end tests for the durable rule registry workflow (SPEC §14.7 3b).

The registry is a thin effect-dispatcher: when a learned rule becomes eligible
it starts that rule's one-time autonomy offer and marks it offered, and
``bless_existing`` grants autonomy by id. Exercised on the time-skipping server
with a mock ``start_autonomy_offer`` activity (the real one is the offer
workflow's own concern).
"""

from __future__ import annotations

import asyncio
import datetime
from typing import TYPE_CHECKING

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.domain.allocations import ProposedCategory, ResolvedCategory
from ynab_agent.domain.effects import FeedRuleLearning, RuleLearningKind
from ynab_agent.domain.enums import (
    DecidedBy,
    RuleSource,
    TrustState,
)
from ynab_agent.domain.ids import CategoryId, RuleId
from ynab_agent.domain.proposal import Decision
from ynab_agent.domain.rule import Rule, RuleAction, RuleMatch
from ynab_agent.learn.registry import RegistryState
from ynab_agent.workflow.registry_types import (
    REGISTRY_WORKFLOW_ID,
    RegistryParams,
)
from ynab_agent.workflow.registry_workflow import RuleRegistryWorkflow
from ynab_agent.workflow.runtime import DATA_CONVERTER

if TYPE_CHECKING:
    from collections.abc import Callable

    from temporalio.client import WorkflowHandle

_TASK_QUEUE = "registry-wf-test"
_SUBS = CategoryId("subscriptions")


def _eligible_rule(rid: str = "r1", payee: str = "Spotify") -> Rule:
    return Rule(
        id=RuleId(rid),
        match=RuleMatch(payee_pattern=payee),
        action=RuleAction(allocation=ProposedCategory(category=_SUBS)),
        trust=TrustState.TRUSTED,
        source=RuleSource.LEARNED,
    )


def _confirm(payee: str = "Spotify") -> FeedRuleLearning:
    return FeedRuleLearning(
        event=RuleLearningKind.CONFIRM,
        payee=payee,
        decision=Decision(
            allocation=ResolvedCategory(category=_SUBS),
            approved=True,
            decided_by=DecidedBy.HUMAN,
            decided_at=datetime.datetime(2026, 5, 31, tzinfo=datetime.UTC),
        ),
    )


def _activities(offered: list[Rule]) -> list[Callable[..., object]]:
    @activity.defn(name="start_autonomy_offer")
    async def start_autonomy_offer(rule: Rule) -> None:
        offered.append(rule)

    @activity.defn(name="alert_failure")
    async def alert_failure(alert: object) -> None:
        return None

    return [start_autonomy_offer, alert_failure]


async def _wait(predicate: Callable[[], bool], tries: int = 60) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never became true")


async def _start(
    env: WorkflowEnvironment, state: RegistryState
) -> WorkflowHandle[RuleRegistryWorkflow, None]:
    return await env.client.start_workflow(
        RuleRegistryWorkflow.run,
        RegistryParams(state=state),
        id=REGISTRY_WORKFLOW_ID,
        task_queue=_TASK_QUEUE,
    )


async def test_eligible_rule_triggers_exactly_one_offer() -> None:
    offered: list[Rule] = []
    rule = _eligible_rule()
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[RuleRegistryWorkflow],
            activities=_activities(offered),
        ),
    ):
        handle = await _start(env, RegistryState(rules=(rule,)))
        await _wait(lambda: len(offered) == 1)
        assert offered[0].id == "r1"

        # The rule is now marked offered, so it drops off the pending list.
        rules = await handle.query(RuleRegistryWorkflow.rules)
        assert rules[0].offered_at is not None

        # A later confirmation of the same payee strengthens the rule but must
        # NOT re-offer it (the one-time guard): offered_at stays set, so the
        # rule never re-enters the pending-offers list.
        await handle.signal(RuleRegistryWorkflow.record, _confirm())
        await asyncio.sleep(0.3)
        assert len(offered) == 1


async def test_bless_existing_grants_autonomy_by_id() -> None:
    offered: list[Rule] = []
    rule = _eligible_rule()
    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[RuleRegistryWorkflow],
            activities=_activities(offered),
        ),
    ):
        handle = await _start(env, RegistryState(rules=(rule,)))
        await _wait(lambda: len(offered) == 1)

        await handle.signal(RuleRegistryWorkflow.bless_existing, "r1")

        async def _blessed() -> bool:
            rules = await handle.query(RuleRegistryWorkflow.rules)
            return rules[0].source is RuleSource.HUMAN_EXPLICIT

        for _ in range(60):
            if await _blessed():
                break
            await asyncio.sleep(0.05)
        rules = await handle.query(RuleRegistryWorkflow.rules)
        assert rules[0].source is RuleSource.HUMAN_EXPLICIT
        assert rules[0].trust is TrustState.TRUSTED
        # Past eligibility now: the on-ramp view no longer lists it.
        view = await handle.query(RuleRegistryWorkflow.view)
        assert view.eligible == ()
