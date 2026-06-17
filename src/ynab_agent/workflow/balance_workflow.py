"""W7 · the budget-balance workflow — propose, confirm by NL, apply (SPEC §8).

One short-lived workflow per (overspent category, period), started by the W6
monitor (id ``balance-offer-{category}-{period}``, ``REJECT_DUPLICATE`` so it is
one offer per period). It asks the model for coverage options, posts them as a
reply on the *same* overspend-alert thread, stamps a ``BalanceThreadId`` search
attribute so W3 routes the reply back here, and acts on it: apply
the chosen plan, decline, or answer a clarifying question and keep waiting.

The apply is the careful part. The workflow reads a baseline snapshot, validates
the moves against real funds and the hard floor, then computes each category's
*absolute* target ``budgeted`` here, in durable workflow state — so re-driving a
write activity re-sets the same value and never double-applies. Every move is
read-back verified before the workflow calls it done.

The seam mirrors the offer/W2 drivers: ``workflow.*`` for all clock reads, side
effects behind activities, pure domain types for the branch — so the model and
mail stacks never enter this sandbox.
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
        render_balance_options,
        render_balance_stale,
        render_balance_unverified,
    )
    from ynab_agent.budget.balance import (
        ApplyMoves,
        BudgetMove,
        DeclineBalance,
        OptionRejection,
        check_moves,
        move_targets,
    )
    from ynab_agent.dispatch.classify import InboundMessage
    from ynab_agent.domain.money import Money
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


def _reason(rejection: OptionRejection) -> str:
    """The owner-facing reason an approved plan can't be applied."""
    if rejection is OptionRejection.OVER_CEILING:
        return "one of the moves is larger than I can make automatically"
    if rejection is OptionRejection.INSUFFICIENT_SOURCE:
        return "a source no longer has enough to cover it"
    if rejection is OptionRejection.SLACK:
        return "a source is now heading over its own budget and can't spare it"
    return "the plan didn't add up"


@workflow.defn
class BudgetBalanceWorkflow:
    """One overspent category's coverage offer for a budget period."""

    def __init__(self) -> None:
        """Start with no reply buffered; ``run`` posts the offer and waits."""
        self._responses: deque[InboundMessage] = deque()
        self._clarifications = 0

    @workflow.signal
    def submit_response(self, message: InboundMessage) -> None:
        """The owner replied on the alert thread (delivered by W3)."""
        self._responses.append(message)

    @workflow.run
    async def run(self, params: BalanceParams) -> BalanceResult:
        """Run the offer, paging the owner on a terminal failure."""
        try:
            return await self._run(params)
        except ActivityError as exc:
            assessment = params.assessment
            await workflow.execute_activity(
                alert_activities.alert_failure,
                build_failure_alert(
                    key=f"balance-{assessment.category}-{params.period}",
                    context=f"budget balance for {assessment.name}",
                    exc=exc,
                ),
                start_to_close_timeout=ALERT_TIMEOUT,
                schedule_to_close_timeout=ALERT_BUDGET,
                retry_policy=ALERT_RETRY,
            )
            raise

    async def _run(self, params: BalanceParams) -> BalanceResult:
        assessment = params.assessment
        label = f"{assessment.category}-{params.period}"
        offer = await workflow.execute_activity(
            balance_activities.propose_balance_options,
            params,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        if not offer.options:
            await self._send(
                params,
                render_balance_could_not_cover(assessment.name),
                f"yb-nocover-{label}",
            )
            return BalanceResult(outcome="could-not-cover")
        options = list(offer.options)

        # Index this workflow by the overspend-alert thread so the owner's reply
        # routes back here (W3 → BalanceThreadId), then post the options as a
        # reply on that same thread — the W6→W7 tie, one conversation (SPEC §8).
        workflow.upsert_search_attributes(
            [_BALANCE_THREAD_ID.value_set(params.thread_id)]
        )
        await self._send(
            params,
            render_balance_options(assessment.name, offer),
            f"yb-cover-{label}",
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
            outcome = await workflow.execute_activity(
                balance_activities.interpret_balance_reply,
                args=[params, message.body, options],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
            if isinstance(outcome, DeclineBalance):
                await self._send(
                    params,
                    render_balance_declined(assessment.name),
                    f"ybalance-declined-{label}",
                )
                return BalanceResult(outcome="declined")
            if isinstance(outcome, ApplyMoves):
                return await self._apply(params, outcome.moves, label)
            # ClarifyBalance: answer, then keep waiting for a clearer reply.
            self._clarifications += 1
            await self._send(
                params,
                outcome.question,
                f"ybalance-clarify-{label}-{self._clarifications}",
            )

    async def _apply(
        self,
        params: BalanceParams,
        moves: tuple[BudgetMove, ...],
        label: str,
    ) -> BalanceResult:
        """Validate, write targets, verify, audit, confirm (SPEC §8)."""
        assessment = params.assessment
        # A reply approved after the budget month rolled over must not apply:
        # the moves were computed against LAST month's figures, and writing
        # them now would silently change the new month's budget while the
        # confirmation talks about the old one.
        period_clock = await workflow.execute_activity(
            monitor_activities.current_period,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        if period_clock.period != params.period:
            await self._send(
                params,
                render_balance_stale(assessment.name),
                f"ybalance-stale-{label}",
            )
            return BalanceResult(outcome="stale-period")
        state = await workflow.execute_activity(
            balance_activities.read_budget_state,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        rejection = check_moves(moves, state.available, slack=state.slack)
        if rejection is not None:
            await self._send(
                params,
                render_balance_failed(assessment.name, _reason(rejection)),
                f"ybalance-failed-{label}",
            )
            return BalanceResult(outcome="rejected", detail=rejection.value)

        # Targets computed here, in durable state, from the baseline snapshot —
        # so a write retry re-sets the same absolute value (no double-apply).
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
                params,
                # Post-write: some moves may have landed — never claim
                # "nothing was changed" after writes started.
                render_balance_unverified(assessment.name),
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
            params,
            render_balance_applied(assessment.name, total),
            f"ybalance-applied-{label}",
        )
        return BalanceResult(outcome="applied")

    async def _send(
        self, params: BalanceParams, body: str, seq_label: str
    ) -> None:
        """Reply on the overspend-alert thread, addressed to the owners (§8).

        Every balancer message — options, clarifications, the apply/decline
        confirmation — replies on the W6 alert thread (``params.thread_id``) so
        the owner sees one conversation. The activity sets the recipients
        explicitly, so a reply on a thread the agent last spoke on still reaches
        the owner. Idempotent on ``seq_label``.
        """
        await workflow.execute_activity(
            balance_activities.send_balance_email,
            args=[params.thread_id, body, seq_label],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
