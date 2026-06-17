"""End-to-end tests for the W7 coordinated budget-balance workflow (§8, #46).

Exercised on the time-skipping server with mock activities. The workflow stamps
a
``BalanceThreadId`` search attribute on the per-period coverage thread, which
the
test server needs registered (as the real cluster does via
manage/search-attributes.yaml). The apply path runs the *real* pure guard
(``check_moves`` / ``move_targets``) and the floor's move cap, so the
floor-refusal, slack, cap, and target-math tests are genuine.
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
    BudgetMove,
    CoordinatedOffer,
    CoverageLine,
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
    CoordinatedReplyResult,
    balance_workflow_id,
)
from ynab_agent.workflow.balance_workflow import CoordinatedBalanceWorkflow
from ynab_agent.workflow.monitor_types import PeriodClock
from ynab_agent.workflow.runtime import DATA_CONVERTER

if TYPE_CHECKING:
    from collections.abc import Callable

_TASK_QUEUE = "balance-wf-test"
_PERIOD = "2026-06"


def _assessment(name: str = "dining") -> OverspendAssessment:
    return OverspendAssessment(
        category=CategoryId(name),
        name=name.title(),
        verdict=OverspendVerdict.TRENDING_OVER,
        budgeted=Money.from_currency("400"),
        spent=Money.from_currency("250"),
        projected=Money.from_currency("520"),
        available=Money.from_currency("150"),
    )


def _params() -> BalanceParams:
    return BalanceParams(assessments=(_assessment(),), period=_PERIOD)


def _offer(amount: str = "120") -> CoordinatedOffer:
    return CoordinatedOffer(
        moves=(
            BudgetMove(
                source=CategoryId("buffer"),
                destination=CategoryId("dining"),
                amount=Money.from_currency(amount),
            ),
        ),
        lines=(
            CoverageLine(
                amount=Money.from_currency(amount),
                destination="Dining",
                source="Buffer",
            ),
        ),
        sources=(
            SourceView(
                category=CategoryId("buffer"),
                name="Buffer",
                slack=Money.from_currency("500"),
            ),
        ),
    )


def _big_offer(moves: int) -> CoordinatedOffer:
    budget_moves = tuple(
        BudgetMove(
            source=CategoryId("@ready-to-assign"),
            destination=CategoryId(f"cat-{i}"),
            amount=Money.from_currency("10"),
        )
        for i in range(moves)
    )
    lines = tuple(
        CoverageLine(
            amount=Money.from_currency("10"),
            destination=f"Cat {i}",
            source="Ready to Assign",
        )
        for i in range(moves)
    )
    return CoordinatedOffer(moves=budget_moves, lines=lines, sources=())


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
        slack={
            CategoryId("buffer"): Money.from_currency("500"),
            CategoryId("dining"): Money.zero(),
        },
    )


def _reply(text: str = "do it") -> InboundMessage:
    return InboundMessage(
        message_id=MessageId("m1"),
        from_address="matthew@example.com",
        subject="re: budget coverage",
        body=text,
        thread_id=ThreadId("thr-coverage"),
        signature_verified=True,
    )


def _result(verdict: str, question: str = "") -> CoordinatedReplyResult:
    return CoordinatedReplyResult(verdict=verdict, question=question)


class _Recorder:
    def __init__(self) -> None:
        self.opened: list[str] = []  # subjects of opened coverage threads
        self.sends: list[str] = []  # reply seq labels
        self.threads: list[str] = []  # thread each reply went on
        self.sets: list[tuple[str, str]] = []  # (category, "$amount")
        self.logged = 0


def _activities(
    *,
    offer: CoordinatedOffer,
    replies: list[CoordinatedReplyResult],
    rec: _Recorder,
    set_ok: bool = True,
    live_period: str = _PERIOD,
    state: BudgetState | None = None,
) -> list[Callable[..., object]]:
    pending = list(replies)
    budget_state = state if state is not None else _state()

    @activity.defn(name="current_period")
    async def current_period() -> PeriodClock:
        return PeriodClock(
            period=live_period,
            clock=MonthClock(day_of_month=15, days_in_month=30),
        )

    @activity.defn(name="propose_coordinated_offer")
    async def propose_coordinated_offer(
        params: BalanceParams,
    ) -> CoordinatedOffer:
        return offer

    @activity.defn(name="send_coordinated_offer")
    async def send_coordinated_offer(
        subject: str, body: str, period: str
    ) -> str:
        rec.opened.append(subject)
        return "thr-coverage"

    @activity.defn(name="interpret_coordinated_reply")
    async def interpret_coordinated_reply(
        reply_text: str, plan_summary: str
    ) -> CoordinatedReplyResult:
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
        rec.threads.append(thread_id)
        rec.sends.append(seq_label)

    @activity.defn(name="alert_failure")
    async def alert_failure(alert: object) -> None:
        return None

    return [
        current_period,
        propose_coordinated_offer,
        send_coordinated_offer,
        interpret_coordinated_reply,
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
    offer: CoordinatedOffer,
    replies: list[CoordinatedReplyResult],
    signals: int,
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
            workflows=[CoordinatedBalanceWorkflow],
            activities=_activities(
                offer=offer,
                replies=replies,
                rec=rec,
                set_ok=set_ok,
                live_period=live_period,
                state=state,
            ),
        ),
    ):
        handle = await env.client.start_workflow(
            CoordinatedBalanceWorkflow.run,
            _params(),
            id=balance_workflow_id(_PERIOD),
            task_queue=_TASK_QUEUE,
        )
        for _ in range(signals):
            await handle.signal(
                CoordinatedBalanceWorkflow.submit_response, _reply()
            )
        result = await handle.result()
    return result, rec


async def test_apply_writes_coordinated_targets_and_confirms() -> None:
    result, rec = await _run(
        offer=_offer(), replies=[_result("apply")], signals=1
    )
    assert result.outcome == "applied"
    # The held plan is applied (not the reply): dining raised to $520, buffer
    # lowered to $380 (absolute targets, idempotent on retry).
    assert ("dining", "$520.00") in rec.sets
    assert ("buffer", "$380.00") in rec.sets
    assert rec.logged == 1
    assert any(s.startswith("ybalance-applied") for s in rec.sends)


async def test_decline_writes_nothing() -> None:
    result, rec = await _run(
        offer=_offer(), replies=[_result("decline")], signals=1
    )
    assert result.outcome == "declined"
    assert rec.sets == []
    assert any(s.startswith("ybalance-declined") for s in rec.sends)


async def test_clarify_then_apply() -> None:
    result, rec = await _run(
        offer=_offer(),
        replies=[_result("clarify", "do it or no thanks?"), _result("apply")],
        signals=2,
    )
    assert result.outcome == "applied"
    assert any(s.startswith("ybalance-clarify") for s in rec.sends)
    assert ("dining", "$520.00") in rec.sets


async def test_nothing_coverable_opens_the_thread_with_why() -> None:
    result, rec = await _run(
        offer=CoordinatedOffer(moves=(), lines=(), sources=()),
        replies=[],
        signals=0,
    )
    assert result.outcome == "could-not-cover"
    assert rec.opened  # the explanation thread was opened
    assert rec.sets == []


async def test_plan_over_the_daily_move_cap_is_refused() -> None:
    # 11 moves exceeds the floor's moves_per_day_cap (10): the plan is refused
    # whole, nothing written (SPEC §0.6, §8, #46).
    result, rec = await _run(
        offer=_big_offer(11), replies=[_result("apply")], signals=1
    )
    assert result.outcome == "over-cap"
    assert rec.sets == []
    assert any(s.startswith("ybalance-cap") for s in rec.sends)


async def test_apply_refused_when_a_move_exceeds_donor_slack() -> None:
    # Re-read at apply time, buffer can only spare $100; the $120 move is funded
    # yet over-slack, so the real check_moves guard refuses it — nothing
    # written.
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
            CategoryId("dining"): Money.zero(),
        },
    )
    result, rec = await _run(
        offer=_offer(),
        replies=[_result("apply")],
        signals=1,
        state=slack_limited,
    )
    assert result.outcome == "rejected"
    assert result.detail == "slack"
    assert rec.sets == []
    assert any(s.startswith("ybalance-failed") for s in rec.sends)


async def test_verify_failure_reports_and_does_not_confirm() -> None:
    result, rec = await _run(
        offer=_offer(), replies=[_result("apply")], signals=1, set_ok=False
    )
    assert result.outcome == "verify-failed"
    assert not any(s.startswith("ybalance-applied") for s in rec.sends)


async def test_no_reply_times_out() -> None:
    result, rec = await _run(offer=_offer(), replies=[], signals=0)
    assert result.outcome == "timed-out"
    assert rec.opened  # the coverage offer was posted before the wait


async def test_offer_opens_a_per_period_thread_and_routes_on_it() -> None:
    """The coverage offer opens one per-period thread and indexes itself by it.

    The plan is posted on a fresh per-period coverage thread,
    ``BalanceThreadId``
    is stamped with it, and every later reply (here the decline) goes on that
    same thread — so the owner's reply routes back here (SPEC §8, #46).
    """
    rec = _Recorder()
    async with (
        await _start_env() as env,
        Worker(
            env.client,
            task_queue=_TASK_QUEUE,
            workflows=[CoordinatedBalanceWorkflow],
            activities=_activities(
                offer=_offer(), replies=[_result("decline")], rec=rec
            ),
        ),
    ):
        handle = await env.client.start_workflow(
            CoordinatedBalanceWorkflow.run,
            _params(),
            id=balance_workflow_id(_PERIOD),
            task_queue=_TASK_QUEUE,
        )
        await handle.signal(
            CoordinatedBalanceWorkflow.submit_response, _reply()
        )
        await handle.result()
        desc = await handle.describe()
    stamped = desc.typed_search_attributes.get(
        SearchAttributeKey.for_keyword("BalanceThreadId")
    )
    assert stamped == "thr-coverage"
    assert rec.threads == ["thr-coverage"]  # the decline replied on it


async def test_approval_after_month_rollover_applies_nothing() -> None:
    # The offer was for 2026-06; the reply lands in July. Applying June's moves
    # would silently rewrite July's budget, so nothing is written.
    result, rec = await _run(
        offer=_offer(),
        replies=[_result("apply")],
        signals=1,
        live_period="2026-07",
    )
    assert result.outcome == "stale-period"
    assert rec.sets == []
    assert any(s.startswith("ybalance-stale") for s in rec.sends)
