"""W7 · the coordinated budget-balance workflow (SPEC §8, #46).

One short-lived workflow per budget period, started by the W6 monitor (id
``balance-offer-{period}``, ``REJECT_DUPLICATE`` so it is one coverage offer per
month). It computes **one** coverage plan over **one** shared, slack-ranked
donor pool covering *every* over/trending category from the pass, opens a
per-period coverage thread, posts the plan, stamps a ``BalanceThreadId`` search
attribute so W3 routes the reply back here, and acts on it: apply the whole
plan, decline, or answer a clarifying question and keep waiting. One plan, one
pool, one apply makes a double-drain (two needs claiming a donor) impossible.

The apply is the careful part. The workflow reads a baseline snapshot, validates
the multi-destination moves against real funds, slack, and the floor's daily
move cap, then computes each category's *absolute* target ``budgeted`` here, in
durable workflow state — so re-driving a write activity re-sets the same value
and never double-applies. Every move is read-back verified before it is done.
"""

from __future__ import annotations

from collections import deque
from datetime import timedelta

from temporalio import workflow
from temporalio.common import SearchAttributeKey
from temporalio.exceptions import ActivityError

from ynab_agent.workflow.balance_types import (
    BALANCE_PATIENCE,
    BALANCE_THREAD_ID,
)

_BALANCE_THREAD_ID = SearchAttributeKey.for_keyword(BALANCE_THREAD_ID)

with workflow.unsafe.imports_passed_through():
    from ynab_agent.agentic.compose import (
        render_balance_applied,
        render_balance_could_not_cover,
        render_balance_declined,
        render_balance_failed,
        render_balance_over_cap,
        render_balance_stale,
        render_balance_unverified,
        render_coordinated_offer,
    )
    from ynab_agent.budget.balance import (
        BudgetMove,
        CoordinatedOffer,
        OptionRejection,
        check_moves,
        move_targets,
    )
    from ynab_agent.budget.message import (
        coverage_subject,
        coverage_uncoverable_subject,
    )
    from ynab_agent.dispatch.classify import InboundMessage
    from ynab_agent.domain.money import Money
    from ynab_agent.policy.floor import CAUTIOUS_FLOOR
    from ynab_agent.workflow import (
        alert_activities,
        balance_activities,
        monitor_activities,
    )
    from ynab_agent.workflow.alerting import build_failure_alert
    from ynab_agent.workflow.balance_types import (
        BalanceParams,
        BalanceResult,
    )
    from ynab_agent.workflow.constants import (
        ACTIVITY_RETRY,
        ACTIVITY_TIMEOUT,
        ALERT_BUDGET,
        ALERT_RETRY,
        ALERT_TIMEOUT,
    )

# The whole budget is the "name" the shared coverage copy addresses — there is
# no single needy category in a coordinated plan.
_BUDGET = "your budget"


def _reason(rejection: OptionRejection) -> str:
    """The owner-facing reason an approved plan can't be applied."""
    if rejection is OptionRejection.OVER_CEILING:
        return "one of the moves is larger than I can make automatically"
    if rejection is OptionRejection.INSUFFICIENT_SOURCE:
        return "a source no longer has enough to cover it"
    if rejection is OptionRejection.SLACK:
        return "a source is now heading over its own budget and can't spare it"
    return "the plan didn't add up"


def _category_count(offer: CoordinatedOffer) -> int:
    """How many categories the offer covers or names as uncovered."""
    covered = {line.destination for line in offer.lines}
    return len(covered) + len(offer.uncovered)


@workflow.defn
class CoordinatedBalanceWorkflow:
    """One budget period's coordinated coverage offer over one pool (#46)."""

    def __init__(self) -> None:
        """Start with no reply buffered; ``run`` posts the offer and waits."""
        self._responses: deque[InboundMessage] = deque()
        self._clarifications = 0

    @workflow.signal
    def submit_response(self, message: InboundMessage) -> None:
        """The owner replied on the coverage thread (delivered by W3)."""
        self._responses.append(message)

    @workflow.run
    async def run(self, params: BalanceParams) -> BalanceResult:
        """Run the coordinated offer, paging the owner on a terminal failure."""
        try:
            return await self._run(params)
        except ActivityError as exc:
            await workflow.execute_activity(
                alert_activities.alert_failure,
                build_failure_alert(
                    key=f"balance-{params.period}",
                    context=f"coordinated budget balance for {params.period}",
                    exc=exc,
                ),
                start_to_close_timeout=ALERT_TIMEOUT,
                schedule_to_close_timeout=ALERT_BUDGET,
                retry_policy=ALERT_RETRY,
            )
            raise

    async def _run(self, params: BalanceParams) -> BalanceResult:
        label = params.period
        offer = await workflow.execute_activity(
            balance_activities.propose_coordinated_offer,
            params,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        if not offer.moves:
            await self._open(
                params,
                coverage_uncoverable_subject(),
                render_balance_could_not_cover(_BUDGET),
            )
            return BalanceResult(outcome="could-not-cover")

        # Open the per-period coverage thread with the plan, then index it by
        # the thread id so the reply routes back here (W3 → BalanceThreadId) —
        # the coordinated W6→W7 tie, one conversation per pass (SPEC §8, #46).
        body = render_coordinated_offer(offer)
        subject = coverage_subject(offer.total, _category_count(offer))
        thread_id = await self._open(params, subject, body)
        workflow.upsert_search_attributes(
            [_BALANCE_THREAD_ID.value_set(thread_id)]
        )

        deadline = workflow.now() + BALANCE_PATIENCE
        while True:
            timeout = deadline - workflow.now()
            if timeout < timedelta(0):
                timeout = timedelta(0)
            try:
                await workflow.wait_condition(
                    lambda: len(self._responses) > 0, timeout=timeout
                )
            except TimeoutError:
                return BalanceResult(outcome="timed-out")
            message = self._responses.popleft()
            reply = await workflow.execute_activity(
                balance_activities.interpret_coordinated_reply,
                args=[message.body, body],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
            if reply.verdict == "decline":
                await self._send(
                    thread_id,
                    render_balance_declined(_BUDGET),
                    f"ybalance-declined-{label}",
                )
                return BalanceResult(outcome="declined")
            if reply.verdict == "apply":
                return await self._apply(params, offer.moves, thread_id, label)
            # clarify: answer, then keep waiting for a clearer reply.
            self._clarifications += 1
            await self._send(
                thread_id,
                reply.question,
                f"ybalance-clarify-{label}-{self._clarifications}",
            )

    async def _open(
        self, params: BalanceParams, subject: str, body: str
    ) -> str:
        """Open the per-period coverage thread with ``body``; return its id."""
        return await workflow.execute_activity(
            balance_activities.send_coordinated_offer,
            args=[subject, body, params.period],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )

    async def _apply(
        self,
        params: BalanceParams,
        moves: tuple[BudgetMove, ...],
        thread_id: str,
        label: str,
    ) -> BalanceResult:
        """Validate, write targets, verify, audit, confirm (SPEC §8, #46)."""
        # A reply approved after the month rolled over must not apply: the moves
        # were computed against last month's figures (SPEC §8).
        period_clock = await workflow.execute_activity(
            monitor_activities.current_period,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        if period_clock.period != params.period:
            await self._send(
                thread_id,
                render_balance_stale(_BUDGET),
                f"ybalance-stale-{label}",
            )
            return BalanceResult(outcome="stale-period")
        # The floor's daily move cap: one coordinated apply per daily pass, so
        # the
        # plan's move count is the day's moves (SPEC §0.6, §8). A plan over the
        # cap
        # is refused whole (the owner can split it), nothing written.
        if len(moves) > CAUTIOUS_FLOOR.moves_per_day_cap:
            await self._send(
                thread_id,
                render_balance_over_cap(
                    len(moves), CAUTIOUS_FLOOR.moves_per_day_cap
                ),
                f"ybalance-cap-{label}",
            )
            return BalanceResult(outcome="over-cap")
        state = await workflow.execute_activity(
            balance_activities.read_budget_state,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        rejection = check_moves(moves, state.available, slack=state.slack)
        if rejection is not None:
            await self._send(
                thread_id,
                render_balance_failed(_BUDGET, _reason(rejection)),
                f"ybalance-failed-{label}",
            )
            return BalanceResult(outcome="rejected", detail=rejection.value)

        # Targets computed here, in durable state, from the baseline snapshot —
        # so a write retry re-sets the same absolute value (no double-apply). A
        # coordinated plan funds several destinations and lowers several
        # sources;
        # ``move_targets`` already collapses that into per-category absolutes.
        targets = move_targets(moves, state.budgeted)
        verified = True
        for category, target in targets.items():
            ok = await workflow.execute_activity(
                balance_activities.set_category_budgeted,
                args=[str(category), target],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
            verified = verified and ok
        if not verified:
            await self._send(
                thread_id,
                render_balance_unverified(_BUDGET),
                f"ybalance-failed-{label}-verify",
            )
            return BalanceResult(outcome="verify-failed")

        await workflow.execute_activity(
            balance_activities.log_budget_moves,
            args=[list(moves), params.period],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        total = Money.zero()
        for move in moves:
            total = total + move.amount
        await self._send(
            thread_id,
            render_balance_applied(_BUDGET, total),
            f"ybalance-applied-{label}",
        )
        return BalanceResult(outcome="applied")

    async def _send(self, thread_id: str, body: str, seq_label: str) -> None:
        """Reply on the per-period coverage thread, addressed to the owners."""
        await workflow.execute_activity(
            balance_activities.send_balance_email,
            args=[thread_id, body, seq_label],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
