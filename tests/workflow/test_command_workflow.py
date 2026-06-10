"""End-to-end tests for the command-confirm workflow (SPEC §5c, §0.6).

A standing command is read back and blessed only on a one-word confirm. Run on
the time-skipping server with mock activities (mirroring the offer workflow): a
yes blesses, a no declines, an unclear reply keeps waiting, and silence ends
without blessing. The workflow stamps the shared ``OfferThreadId`` on open, so
the env registers it (as the cluster does via manage/search-attributes.yaml).
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
from ynab_agent.domain.enums import OfferVerdict
from ynab_agent.domain.ids import CategoryId, MessageId, ThreadId
from ynab_agent.domain.rule import RuleAction, RuleMatch
from ynab_agent.learn.events import ExplicitCommand
from ynab_agent.workflow.command_types import CommandConfirmParams
from ynab_agent.workflow.command_workflow import CommandConfirmWorkflow
from ynab_agent.workflow.runtime import DATA_CONVERTER

if TYPE_CHECKING:
    from collections.abc import Callable

_TASK_QUEUE = "command-wf-test"


def _command() -> ExplicitCommand:
    return ExplicitCommand(
        match=RuleMatch(payee_pattern="Costco"),
        action=RuleAction(
            allocation=ProposedCategory(category=CategoryId("groceries"))
        ),
    )


def _reply(text: str = "yes do it") -> InboundMessage:
    return InboundMessage(
        message_id=MessageId("m1"),
        from_address="matthew@example.com",
        subject="re: Confirm: always Costco as Groceries?",
        body=text,
        thread_id=ThreadId("thr-cmd"),
        signature_verified=True,
    )


def _activities(
    *, verdicts: list[OfferVerdict], calls: list[str]
) -> list[Callable[..., object]]:
    seq = list(verdicts)

    @activity.defn(name="open_command_thread")
    async def open_command_thread(command: ExplicitCommand) -> str:
        calls.append("open")
        return "thr-cmd"

    @activity.defn(name="interpret_offer_reply")
    async def interpret_offer_reply(
        reply_text: str, payee: str
    ) -> OfferVerdict:
        return seq.pop(0)

    @activity.defn(name="accept_command")
    async def accept_command(command: ExplicitCommand, thread_id: str) -> None:
        calls.append(f"accept:{thread_id}")

    @activity.defn(name="decline_command")
    async def decline_command(command: ExplicitCommand, thread_id: str) -> None:
        calls.append(f"decline:{thread_id}")

    @activity.defn(name="clarify_offer")
    async def clarify_offer(
        payee: str, thread_id: str, message_id: str
    ) -> None:
        calls.append(f"clarify:{thread_id}:{message_id}")

    @activity.defn(name="alert_failure")
    async def alert_failure(alert: object) -> None:
        return None

    return [
        open_command_thread,
        interpret_offer_reply,
        accept_command,
        decline_command,
        clarify_offer,
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
            workflows=[CommandConfirmWorkflow],
            activities=_activities(verdicts=verdicts, calls=calls),
        ),
    ):
        handle = await env.client.start_workflow(
            CommandConfirmWorkflow.run,
            CommandConfirmParams(command=_command()),
            id="command-confirm-test",
            task_queue=_TASK_QUEUE,
        )
        for _ in range(replies):
            await handle.signal(
                CommandConfirmWorkflow.submit_response, _reply()
            )
        await handle.result()
    return calls


async def test_confirm_blesses_only_after_a_yes() -> None:
    calls = await _run(verdicts=[OfferVerdict.ACCEPT], replies=1)
    assert "open" in calls
    assert "accept:thr-cmd" in calls
    assert not any(c.startswith("decline") for c in calls)


async def test_decline_does_not_bless() -> None:
    calls = await _run(verdicts=[OfferVerdict.DECLINE], replies=1)
    assert "decline:thr-cmd" in calls
    assert not any(c.startswith("accept") for c in calls)


async def test_unclear_keeps_waiting_then_a_clear_yes_blesses() -> None:
    # The unclear reply is acknowledged (a YES/NO restatement), then the
    # clear yes blesses.
    calls = await _run(
        verdicts=[OfferVerdict.UNCLEAR, OfferVerdict.ACCEPT], replies=2
    )
    assert any(c.startswith("clarify:thr-cmd") for c in calls)
    assert "accept:thr-cmd" in calls
    assert not any(c.startswith("decline") for c in calls)


async def test_silence_never_blesses() -> None:
    # No confirm at all: the patience window elapses (time-skipped) and the
    # command is never blessed — the read-back alone grants nothing.
    calls = await _run(verdicts=[], replies=0)
    assert calls == ["open"]


def test_command_confirm_id_is_stable_per_payee_category() -> None:
    from ynab_agent.workflow.command_types import command_confirm_id

    same = command_confirm_id(_command())
    assert same == command_confirm_id(
        _command()
    )  # deterministic: dedups resends
    assert same.startswith("command-confirm-")
    other = ExplicitCommand(
        match=RuleMatch(payee_pattern="Costco"),
        action=RuleAction(
            allocation=ProposedCategory(category=CategoryId("dining"))
        ),
    )
    assert command_confirm_id(other) != same  # a different target → fresh id
