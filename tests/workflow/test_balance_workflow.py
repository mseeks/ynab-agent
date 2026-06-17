"""End-to-end tests for the W7 budget-balance workflow (SPEC §8).

Exercised on the time-skipping server with mock activities. The workflow stamps
a ``BalanceThreadId`` search attribute on offer, which the test server needs
registered (as the real cluster does via manage/search-attributes.yaml). The
apply path runs the *real* pure guard (``check_moves`` / ``move_targets``), so
the floor-refusal and target-math tests are genuine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from temporalio import activity
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.common import SearchAttributeKey
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ynab_agent.budget.balance import (
    ApplyMoves,
    BalanceOffer,
    BalanceOption,
    BalanceOutcome,
    BudgetMove,
    ClarifyBalance,
    DeclineBalance,
    SourceView,
)
from ynab_agent.budget.overspend import (
    MonthClock,
    OverspendAssessment,
    OverspendVerdict,
)
from ynab_agent.dispatch.classify import InboundMessage
from ynab_agent.domain.ids import CategoryId, MessageId, ThreadId
from ynab_agent.domain.money import Money
from ynab_agent.workflow.balance_types import (
    BalanceParams,
    BalanceResult,
    BudgetState,
    balance_workflow_id,
)
from ynab_agent.workflow.balance_workflow import BudgetBalanceWorkflow
from ynab_agent.workflow.monitor_types import PeriodClock
from ynab_agent.workflow.runtime import DATA_CONVERTER

if TYPE_CHECKING:
    from collections.abc import Callable

_TASK_QUEUE = "balance-wf-test"
_PERIOD = "2026-06"


def _assessment() -> OverspendAssessment:
    # available $150 (zero rollover: budgeted - spent) → a $120 need
    # (projected remaining $270 less $150 on hand), as before #44.
    return OverspendAssessment(
        category=CategoryId("dining"),
        name="Dining Out",
        verdict=OverspendVerdict.TRENDING_OVER,
        budgeted=Money.from_currency("400"),
        spent=Money.from_currency("250"),
        projected=Money.from_currency("520"),
        available=Money.from_currency("150"),
    )


def _params() -> BalanceParams:
    return BalanceParams(
        assessment=_assessment(), thread_id="thr-overspend", period=_PERIOD
    )


def _option(amount: str = "120") -> BalanceOption:
    return BalanceOption(
        label="From Buffer",
        moves=(
            BudgetMove(
                source=CategoryId("buffer"),
                destination=CategoryId("dining"),
                amount=Money.from_currency(amount),
            ),
        ),
        rationale="Buffer has plenty.",
    )


def _apply_moves(amount: str) -> ApplyMoves:
    return ApplyMoves(
        moves=(
            BudgetMove(
                source=CategoryId("buffer"),
                destination=CategoryId("dining"),
                amount=Money.from_currency(amount),
            ),
        )
    )


def _state() -> BudgetState:
    return BudgetState(
        available={
            CategoryId("buffer"): Money.from_currency("500"),
            CategoryId("dining"): Money.from_currency("-20"),
        },
        budgeted={
            CategoryId("buffer"): Money.from_currency("500"),
            CategoryId("dining"): Money.from_currency("400"),
        },
    )


def _reply(text: str = "do it") -> InboundMessage:
    return InboundMessage(
        message_id=MessageId("m1"),
        from_address="matthew@example.com",
        subject="re: Dining Out: trending over budget",
        body=text,
        thread_id=ThreadId("thr-overspend"),
        signature_verified=True,
    )


class _Recorder:
    def __init__(self) -> None:
        self.sends: list[str] = []
        self.threads: list[str] = []  # thread each balance email replied on
        self.sent: list[str] = []  # the bodies, for copy assertions
        self.sets: list[tuple[str, str]] = []  # (category, "$amount")
        self.logged = 0


def _activities(
    *,
    options: list[BalanceOption],
    outcomes: list[BalanceOutcome],
    rec: _Recorder,
    set_ok: bool = True,
    live_period: str = _PERIOD,
    state: BudgetState | None = None,
) -> list[Callable[..., object]]:
    pending = list(outcomes)
    budget_state = state if state is not None else _state()

    @activity.defn(name="current_period")
    async def current_period() -> PeriodClock:
        return PeriodClock(
            period=live_period,
            clock=MonthClock(day_of_month=15, days_in_month=30),
        )

    @activity.defn(name="propose_balance_options")
    async def propose_balance_options(
        params: BalanceParams,
    ) -> BalanceOffer:
        return BalanceOffer(
            options=tuple(options),
            sources=(
                SourceView(
                    category=CategoryId("buffer"),
                    name="Buffer",
                    slack=Money.from_currency("500"),
                ),
            ),
        )

    @activity.defn(name="interpret_balance_reply")
    async def interpret_balance_reply(
        params: BalanceParams, reply_text: str, opts: list[BalanceOption]
    ) -> BalanceOutcome:
        return pending.pop(0)

    @activity.defn(name="read_budget_state")
    async def read_budget_state() -> BudgetState:
        return budget_state

    @activity.defn(name="set_category_budgeted")
    async def set_category_budgeted(category_id: str, target: Money) -> bool:
        rec.sets.append((category_id, str(target)))
        return set_ok

    @activity.defn(name="log_budget_moves")
    async def log_budget_moves(moves: list[BudgetMove], period: str) -> None:
        rec.logged += 1

    @activity.defn(name="send_balance_email")
    async def send_balance_email(
        thread_id: str, body: str, seq_label: str
    ) -> None:
        rec.sends.append(seq_label)
        rec.threads.append(thread_id)
        rec.sent.append(body)

    @activity.defn(name="alert_failure")
    async def alert_failure(alert: object) -> None:
        return None

    return [
        current_period,
        propose_balance_options,
        interpret_balance_reply,
        read_budget_state,
        set_category_budgeted,
        log_budget_moves,
        send_balance_email,
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
                "BalanceThreadId": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD
            },
        )
    )
    return env


async def _run(
    *,
    options: list[BalanceOption],
    outcomes: list[BalanceOutcome],
    replies: int,
    set_ok: bool = True,
    live_period: str = _PERIOD,
    state: BudgetState | None = None,
) -> tuple[BalanceResult, _Recorder]:
    rec = _Recorder()
    async with (
        await _start_env() as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[BudgetBalanceWorkflow],
            activities=_activities(
                options=options,
                outcomes=outcomes,
                rec=rec,
                set_ok=set_ok,
                live_period=live_period,
                state=state,
            ),
        ),
    ):
        handle = await env.client.start_workflow(
            BudgetBalanceWorkflow.run,
            _params(),
            id=balance_workflow_id("dining", _PERIOD),
            task_queue=_TASK_QUEUE,
        )
        for _ in range(replies):
            await handle.signal(BudgetBalanceWorkflow.submit_response, _reply())
        result = await handle.result()
    return result, rec


async def test_apply_writes_targets_verifies_and_confirms() -> None:
    result, rec = await _run(
        options=[_option()], outcomes=[_apply_moves("120")], replies=1
    )
    assert result.outcome == "applied"
    # Destination raised to $520, source lowered to $380 (absolute targets).
    assert ("dining", "$520.00") in rec.sets
    assert ("buffer", "$380.00") in rec.sets
    assert rec.logged == 1
    assert any(s.startswith("ybalance-applied") for s in rec.sends)


async def test_natural_language_modified_plan_is_applied() -> None:
    # The owner asked to cover only $50; the workflow applies the modified plan.
    result, rec = await _run(
        options=[_option()], outcomes=[_apply_moves("50")], replies=1
    )
    assert result.outcome == "applied"
    assert ("dining", "$450.00") in rec.sets  # 400 + 50
    assert ("buffer", "$450.00") in rec.sets  # 500 - 50


async def test_over_ceiling_move_is_refused_by_the_floor() -> None:
    # A $600 move exceeds the $500 per-move ceiling: the real check_moves guard
    # refuses it, nothing is written, even though the owner "approved" it.
    result, rec = await _run(
        options=[_option()], outcomes=[_apply_moves("600")], replies=1
    )
    assert result.outcome == "rejected"
    assert result.detail == "over_ceiling"
    assert rec.sets == []  # no writes
    assert any(s.startswith("ybalance-failed") for s in rec.sends)


async def test_apply_refused_when_a_move_exceeds_donor_slack() -> None:
    # Buffer holds $500 but, re-read at apply time, can only spare $100 of slack
    # (its own spend has grown). The owner's $120 move is funded yet over-slack,
    # so the real check_moves guard refuses it — nothing is written.
    slack_limited = BudgetState(
        available={
            CategoryId("buffer"): Money.from_currency("500"),
            CategoryId("dining"): Money.from_currency("-20"),
        },
        budgeted={
            CategoryId("buffer"): Money.from_currency("500"),
            CategoryId("dining"): Money.from_currency("400"),
        },
        slack={
            CategoryId("buffer"): Money.from_currency("100"),
            CategoryId("dining"): Money.from_currency("0"),
        },
    )
    result, rec = await _run(
        options=[_option()],
        outcomes=[_apply_moves("120")],
        replies=1,
        state=slack_limited,
    )
    assert result.outcome == "rejected"
    assert result.detail == "slack"
    assert rec.sets == []  # no writes
    assert any(s.startswith("ybalance-failed") for s in rec.sends)


async def test_decline_sends_a_note_and_writes_nothing() -> None:
    result, rec = await _run(
        options=[_option()], outcomes=[DeclineBalance()], replies=1
    )
    assert result.outcome == "declined"
    assert rec.sets == []
    assert any(s.startswith("ybalance-declined") for s in rec.sends)


async def test_clarify_then_apply() -> None:
    result, rec = await _run(
        options=[_option()],
        outcomes=[ClarifyBalance(question="From which?"), _apply_moves("120")],
        replies=2,
    )
    assert result.outcome == "applied"
    assert any(s.startswith("ybalance-clarify") for s in rec.sends)
    assert ("dining", "$520.00") in rec.sets


async def test_no_feasible_options_sends_could_not_cover() -> None:
    result, rec = await _run(options=[], outcomes=[], replies=0)
    assert result.outcome == "could-not-cover"
    assert any(s.startswith("yb-nocover") for s in rec.sends)
    assert rec.sets == []


async def test_verify_failure_reports_and_does_not_confirm() -> None:
    result, rec = await _run(
        options=[_option()],
        outcomes=[_apply_moves("120")],
        replies=1,
        set_ok=False,
    )
    assert result.outcome == "verify-failed"
    assert any("failed" in s for s in rec.sends)
    assert not any(s.startswith("ybalance-applied") for s in rec.sends)


async def test_no_reply_times_out() -> None:
    result, rec = await _run(options=[_option()], outcomes=[], replies=0)
    # Wait — replies=0 with options posts the offer then the patience window
    # elapses (time-skipped) with no answer.
    assert result.outcome == "timed-out"
    assert any(s.startswith("yb-cover") for s in rec.sends)


async def test_offer_posts_and_routes_on_the_alert_thread() -> None:
    """The balancer posts on the W6 alert thread and indexes itself by it.

    Every balancer email replies on ``thread_id`` (the overspend-alert thread,
    ``"thr-overspend"``), and ``BalanceThreadId`` is stamped with that same
    thread — so the owner sees one conversation and a reply on it routes back
    here (the W6→W7 tie, SPEC §8). This is the regression guard against posting
    on a freshly opened thread instead.
    """
    rec = _Recorder()
    async with (
        await _start_env() as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[BudgetBalanceWorkflow],
            activities=_activities(
                options=[_option()], outcomes=[DeclineBalance()], rec=rec
            ),
        ),
    ):
        handle = await env.client.start_workflow(
            BudgetBalanceWorkflow.run,
            _params(),
            id=balance_workflow_id("dining", _PERIOD),
            task_queue=_TASK_QUEUE,
        )
        await handle.signal(BudgetBalanceWorkflow.submit_response, _reply())
        await handle.result()
        desc = await handle.describe()
    stamped = desc.typed_search_attributes.get(
        SearchAttributeKey.for_keyword("BalanceThreadId")
    )
    assert stamped == "thr-overspend"
    # The options offer and the decline confirmation both replied on the alert
    # thread — never on a separately opened one.
    assert rec.threads == ["thr-overspend", "thr-overspend"]


async def test_approval_after_month_rollover_applies_nothing() -> None:
    # The offer was for 2026-06; the reply lands in July. The moves were
    # computed against June's figures — applying them would silently rewrite
    # JULY's budget, so nothing is written and the note says why.
    result, rec = await _run(
        options=[_option()],
        outcomes=[_apply_moves("120")],
        replies=1,
        live_period="2026-07",
    )
    assert result.outcome == "stale-period"
    assert rec.sets == []  # no budget write happened
    assert any("month ended" in body for body in rec.sent)


async def test_verify_failure_says_some_moves_may_have_landed() -> None:
    # Post-write failure: never claim "nothing was changed" once writes have
    # started — point the owner at YNAB instead.
    result, rec = await _run(
        options=[_option()],
        outcomes=[_apply_moves("120")],
        replies=1,
        set_ok=False,
    )
    assert result.outcome == "verify-failed"
    assert any("couldn't confirm" in body for body in rec.sent)
    assert not any("Nothing was changed" in body for body in rec.sent)
