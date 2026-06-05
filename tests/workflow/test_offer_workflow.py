"""End-to-end tests for the autonomy-offer workflow (SPEC §14.7 3b).

Exercised on the time-skipping server with mock activities. The workflow stamps
an ``OfferThreadId`` search attribute on open, which the test server hangs on
unless registered — so the env registers it (as the real cluster does via
manage/search-attributes.yaml), and a clean run also proves the stamp path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from temporalio import activity
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.dispatch.classify import InboundMessage
from ynab_agent.domain.allocations import ProposedCategory
from ynab_agent.domain.enums import OfferVerdict, RuleSource, TrustState
from ynab_agent.domain.ids import CategoryId, MessageId, RuleId, ThreadId
from ynab_agent.domain.rule import Rule, RuleAction, RuleMatch
from ynab_agent.workflow.offer_types import OfferParams, offer_workflow_id
from ynab_agent.workflow.offer_workflow import AutonomyOfferWorkflow
from ynab_agent.workflow.runtime import DATA_CONVERTER

if TYPE_CHECKING:
    from collections.abc import Callable

_TASK_QUEUE = "offer-wf-test"


def _rule() -> Rule:
    return Rule(
        id=RuleId("r1"),
        match=RuleMatch(payee_pattern="Spotify"),
        action=RuleAction(
            allocation=ProposedCategory(category=CategoryId("subscriptions"))
        ),
        trust=TrustState.TRUSTED,
        source=RuleSource.LEARNED,
    )


def _reply(text: str = "yes please") -> InboundMessage:
    return InboundMessage(
        message_id=MessageId("m1"),
        from_address="matthew@example.com",
        subject="re: Auto-handle Spotify?",
        body=text,
        thread_id=ThreadId("thr-offer"),
        signature_verified=True,
    )


def _activities(
    *, verdicts: list[OfferVerdict], calls: list[str]
) -> list[Callable[..., object]]:
    seq = list(verdicts)

    @activity.defn(name="open_offer_thread")
    async def open_offer_thread(rule: Rule) -> str:
        calls.append("open")
        return "thr-offer"

    @activity.defn(name="interpret_offer_reply")
    async def interpret_offer_reply(
        reply_text: str, payee: str
    ) -> OfferVerdict:
        return seq.pop(0)

    @activity.defn(name="accept_offer")
    async def accept_offer(rule: Rule, thread_id: str) -> None:
        calls.append(f"accept:{thread_id}")

    @activity.defn(name="decline_offer")
    async def decline_offer(rule: Rule, thread_id: str) -> None:
        calls.append(f"decline:{thread_id}")

    @activity.defn(name="alert_failure")
    async def alert_failure(alert: object) -> None:
        return None

    return [
        open_offer_thread,
        interpret_offer_reply,
        accept_offer,
        decline_offer,
        alert_failure,
    ]


async def _start_env() -> WorkflowEnvironment:
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    )
    await env.client.operator_service.add_search_attributes(
        AddSearchAttributesRequest(
            namespace="default",
            search_attributes={
                "OfferThreadId": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD
            },
        )
    )
    return env


async def _run(*, verdicts: list[OfferVerdict], replies: int) -> list[str]:
    calls: list[str] = []
    async with (
        await _start_env() as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[AutonomyOfferWorkflow],
            activities=_activities(verdicts=verdicts, calls=calls),
        ),
    ):
        handle = await env.client.start_workflow(
            AutonomyOfferWorkflow.run,
            OfferParams(rule=_rule()),
            id=offer_workflow_id("r1"),
            task_queue=_TASK_QUEUE,
        )
        for _ in range(replies):
            await handle.signal(AutonomyOfferWorkflow.submit_response, _reply())
        await handle.result()
    return calls


async def test_accept_blesses_and_confirms() -> None:
    calls = await _run(verdicts=[OfferVerdict.ACCEPT], replies=1)
    assert "open" in calls
    assert "accept:thr-offer" in calls
    assert not any(c.startswith("decline") for c in calls)


async def test_decline_sends_the_keep_proposing_note() -> None:
    calls = await _run(verdicts=[OfferVerdict.DECLINE], replies=1)
    assert "decline:thr-offer" in calls
    assert not any(c.startswith("accept") for c in calls)


async def test_unclear_keeps_waiting_then_a_clear_yes_accepts() -> None:
    # First reply is unclear (no bless), a later clear yes accepts.
    calls = await _run(
        verdicts=[OfferVerdict.UNCLEAR, OfferVerdict.ACCEPT], replies=2
    )
    assert "accept:thr-offer" in calls
    assert not any(c.startswith("decline") for c in calls)


async def test_timeout_ends_the_offer_without_blessing() -> None:
    # No reply at all: the patience window elapses (time-skipped) and the offer
    # ends without accepting or declining — the rule stays merely eligible.
    calls = await _run(verdicts=[], replies=0)
    assert calls == ["open"]
