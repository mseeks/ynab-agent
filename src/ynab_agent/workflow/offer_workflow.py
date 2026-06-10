"""The autonomy-offer workflow — the proactive eligibility prompt (§14.7 3b).

One short-lived workflow per eligible rule (id ``autonomy-offer-{rule_id}``,
started ``REJECT_DUPLICATE`` so the prompt is one-time). It opens its own email
thread asking "want me to auto-handle *Payee*?", stamps an ``OfferThreadId``
search attribute so W3 routes the reply back here (a bless-acceptance, never a
category reply), and waits for an answer. A yes blesses the rule and confirms; a
no sends a brief note; an unclear reply keeps waiting; silence past the patience
window simply ends the offer (the owner can still bless later via a command).

The seam mirrors the W2 driver: ``workflow.*`` for all clock reads, side effects
behind activities, pure :class:`~ynab_agent.domain.enums.OfferVerdict` for the
branch — so the model/mail stacks never enter this sandbox.
"""

from __future__ import annotations

from collections import deque
from datetime import timedelta

from temporalio import workflow
from temporalio.common import SearchAttributeKey
from temporalio.exceptions import ActivityError

from ynab_agent.workflow.offer_types import OFFER_PATIENCE, OFFER_THREAD_ID

_OFFER_THREAD_ID = SearchAttributeKey.for_keyword(OFFER_THREAD_ID)

with workflow.unsafe.imports_passed_through():
    from ynab_agent.dispatch.classify import InboundMessage
    from ynab_agent.domain.enums import OfferVerdict
    from ynab_agent.workflow import alert_activities, offer_activities
    from ynab_agent.workflow.alerting import build_failure_alert
    from ynab_agent.workflow.constants import (
        ACTIVITY_RETRY,
        ACTIVITY_TIMEOUT,
        ALERT_BUDGET,
        ALERT_RETRY,
        ALERT_TIMEOUT,
    )
    from ynab_agent.workflow.offer_types import OfferParams


@workflow.defn
class AutonomyOfferWorkflow:
    """One eligible rule's one-time "may I auto-handle this?" offer."""

    def __init__(self) -> None:
        """Start with no reply buffered; ``run`` opens the thread and waits."""
        self._responses: deque[InboundMessage] = deque()

    @workflow.signal
    def submit_response(self, message: InboundMessage) -> None:
        """The owner replied to the offer (delivered by W3)."""
        self._responses.append(message)

    @workflow.run
    async def run(self, params: OfferParams) -> None:
        """Make the offer and act on the reply, paging on terminal failure."""
        try:
            await self._run(params)
        except ActivityError as exc:
            payee = params.rule.match.payee_pattern
            await workflow.execute_activity(
                alert_activities.alert_failure,
                build_failure_alert(
                    key=f"offer-{params.rule.id}",
                    context=f"autonomy offer for {payee}",
                    exc=exc,
                ),
                start_to_close_timeout=ALERT_TIMEOUT,
                schedule_to_close_timeout=ALERT_BUDGET,
                retry_policy=ALERT_RETRY,
            )
            raise

    async def _run(self, params: OfferParams) -> None:
        rule = params.rule
        thread_id = await workflow.execute_activity(
            offer_activities.open_offer_thread,
            rule,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        # Index this workflow by its thread so a reply routes here (§5a-style).
        workflow.upsert_search_attributes(
            [_OFFER_THREAD_ID.value_set(thread_id)]
        )

        deadline = workflow.now() + OFFER_PATIENCE
        while True:
            timeout = deadline - workflow.now()
            if timeout < timedelta(0):
                timeout = timedelta(0)
            try:
                await workflow.wait_condition(
                    lambda: len(self._responses) > 0, timeout=timeout
                )
            except TimeoutError:
                # No clear answer in the window; leave the rule eligible, end.
                return
            message = self._responses.popleft()
            verdict = await workflow.execute_activity(
                offer_activities.interpret_offer_reply,
                args=[message.body, rule.match.payee_pattern],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
            if verdict is OfferVerdict.ACCEPT:
                await workflow.execute_activity(
                    offer_activities.accept_offer,
                    args=[rule, thread_id],
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=ACTIVITY_RETRY,
                )
                return
            if verdict is OfferVerdict.DECLINE:
                await workflow.execute_activity(
                    offer_activities.decline_offer,
                    args=[rule, thread_id],
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=ACTIVITY_RETRY,
                )
                return
            # UNCLEAR: acknowledge (the owner spoke — silence reads as a
            # black hole) and keep waiting for a clearer reply.
            await workflow.execute_activity(
                offer_activities.clarify_offer,
                args=[
                    rule.match.payee_pattern,
                    thread_id,
                    str(message.message_id),
                ],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
