"""The command-confirm workflow — read-back before blessing (SPEC §5c, §0.6).

A standing command ("always categorize X as Y") grants standing autonomy, so it
is not blessed inline. This short-lived workflow opens a read-back thread that
echoes the interpretation, stamps the shared ``OfferThreadId`` search attribute
so W3 routes the reply back here (no new dispatch path — a command-confirm reply
is an autonomy decision, like an offer reply), and waits for a one-word confirm.
A yes blesses the rule (the registry ``bless``) and confirms; a no sends a brief
note; an unclear reply keeps waiting; silence past the patience window simply
never blesses.

Mirrors :class:`~ynab_agent.workflow.offer_workflow.AutonomyOfferWorkflow` (the
proactive prompt); the difference is only *what* a yes blesses — an explicit
(payee, category) command rather than an already-eligible rule — so the reply
interpreter (``offer_activities.interpret_offer_reply``) is shared.
"""

from __future__ import annotations

from collections import deque
from datetime import timedelta

from temporalio import workflow
from temporalio.common import SearchAttributeKey
from temporalio.exceptions import ActivityError

from ynab_agent.workflow.offer_types import OFFER_THREAD_ID

_OFFER_THREAD_ID = SearchAttributeKey.for_keyword(OFFER_THREAD_ID)

with workflow.unsafe.imports_passed_through():
    from ynab_agent.dispatch.classify import InboundMessage
    from ynab_agent.domain.enums import OfferVerdict
    from ynab_agent.workflow import (
        alert_activities,
        command_activities,
        offer_activities,
    )
    from ynab_agent.workflow.alerting import build_failure_alert
    from ynab_agent.workflow.command_types import (
        COMMAND_CONFIRM_PATIENCE,
        CommandConfirmParams,
    )
    from ynab_agent.workflow.constants import (
        ACTIVITY_RETRY,
        ACTIVITY_TIMEOUT,
        ALERT_BUDGET,
        ALERT_RETRY,
        ALERT_TIMEOUT,
    )


@workflow.defn
class CommandConfirmWorkflow:
    """One standing command's read-back + one-word confirm before blessing."""

    def __init__(self) -> None:
        """Start with no reply buffered; ``run`` opens the thread and waits."""
        self._responses: deque[InboundMessage] = deque()

    @workflow.signal
    def submit_response(self, message: InboundMessage) -> None:
        """The owner replied to the read-back (delivered by W3)."""
        self._responses.append(message)

    @workflow.run
    async def run(self, params: CommandConfirmParams) -> None:
        """Read back the command and act on the reply, paging on failure."""
        try:
            await self._run(params)
        except ActivityError as exc:
            payee = params.command.match.payee_pattern
            await workflow.execute_activity(
                alert_activities.alert_failure,
                build_failure_alert(
                    key=f"command-confirm-{payee}",
                    context=f"command confirm for {payee}",
                    exc=exc,
                ),
                start_to_close_timeout=ALERT_TIMEOUT,
                schedule_to_close_timeout=ALERT_BUDGET,
                retry_policy=ALERT_RETRY,
            )
            raise

    async def _run(self, params: CommandConfirmParams) -> None:
        command = params.command
        thread_id = await workflow.execute_activity(
            command_activities.open_command_thread,
            command,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY,
        )
        # Index this workflow by its thread so a reply routes here, reusing the
        # offer's routing (a command-confirm reply is an autonomy decision).
        workflow.upsert_search_attributes(
            [_OFFER_THREAD_ID.value_set(thread_id)]
        )

        deadline = workflow.now() + COMMAND_CONFIRM_PATIENCE
        while True:
            timeout = deadline - workflow.now()
            if timeout < timedelta(0):
                timeout = timedelta(0)
            try:
                await workflow.wait_condition(
                    lambda: len(self._responses) > 0, timeout=timeout
                )
            except TimeoutError:
                # No clear confirm in the window; never bless, end.
                return
            message = self._responses.popleft()
            verdict = await workflow.execute_activity(
                offer_activities.interpret_offer_reply,
                args=[message.body, command.match.payee_pattern],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
            if verdict is OfferVerdict.ACCEPT:
                await workflow.execute_activity(
                    command_activities.accept_command,
                    args=[command, thread_id],
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=ACTIVITY_RETRY,
                )
                return
            if verdict is OfferVerdict.DECLINE:
                await workflow.execute_activity(
                    command_activities.decline_command,
                    args=[command, thread_id],
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=ACTIVITY_RETRY,
                )
                return
            # UNCLEAR: acknowledge (the owner spoke — silence reads as a
            # black hole) and keep waiting for a clearer reply.
            await workflow.execute_activity(
                offer_activities.clarify_offer,
                args=[
                    command.match.payee_pattern,
                    thread_id,
                    str(message.message_id),
                ],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
