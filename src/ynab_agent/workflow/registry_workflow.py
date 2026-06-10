"""W5 · the durable rule registry — the agent's persistent learning memory.

A singleton, long-lived workflow (id :data:`REGISTRY_WORKFLOW_ID`) holding the
learned rule table as Temporal state (SPEC §14, §0.5 derived-state). It is born
on the first learning signal — the ``feed_rule_learning`` activity does a
signal-with-start — and lives forever, continuing-as-new to keep its history
bounded while carrying the rule table forward.

The workflow is a thin durable shell over the pure
:mod:`ynab_agent.learn.registry` folds (the same pattern ``txn_workflow`` has
over ``state_machine``): signals fold confirm/correct/bless inputs into the
state; queries read it back for the autonomy gate and the on-ramp prompt. All
clock and id reads go through ``workflow.*`` so replay stays deterministic.
"""

from __future__ import annotations

import contextlib

from temporalio import workflow
from temporalio.exceptions import ActivityError

from ynab_agent.workflow.registry_types import RegistryParams, RegistryView

with workflow.unsafe.imports_passed_through():
    from ynab_agent.domain.effects import FeedRuleLearning
    from ynab_agent.domain.ids import RuleId
    from ynab_agent.domain.rule import Rule
    from ynab_agent.learn.events import ExplicitCommand
    from ynab_agent.learn.registry import (
        RegistryState,
        bless_by_id,
        bless_rule,
        eligible_for_bless,
        mark_offered,
        pending_offers,
        record_learning,
        revoke_payee,
        rules_for_payee,
    )
    from ynab_agent.workflow import alert_activities, offer_activities
    from ynab_agent.workflow.alerting import build_failure_alert
    from ynab_agent.workflow.constants import (
        ACTIVITY_RETRY,
        ACTIVITY_TIMEOUT,
        ALERT_BUDGET,
        ALERT_RETRY,
        ALERT_TIMEOUT,
    )


@workflow.defn
class RuleRegistryWorkflow:
    """The household's one durable rule table (one fold per learning signal)."""

    def __init__(self) -> None:
        """Start empty; the run method adopts any carried-forward state."""
        self._state = RegistryState()

    @workflow.run
    async def run(self, params: RegistryParams) -> None:
        """Hold the rule table; volunteer offers; roll when history grows.

        A thin effect-dispatcher (mirroring the W2 driver): each wake, if a rule
        has newly become eligible and was never offered, start its one-time
        autonomy offer (SPEC §14.7 3b) and mark it offered; otherwise, once the
        offer queue is drained and history has grown, continue-as-new carrying
        the table forward. Draining before the roll mirrors W2 draining its
        inbound before continue-as-new.
        """
        self._state = params.state
        while True:
            await workflow.wait_condition(
                lambda: (
                    bool(pending_offers(self._state))
                    or workflow.info().is_continue_as_new_suggested()
                )
            )
            pending = pending_offers(self._state)
            if pending:
                await self._offer(pending[0])
                continue
            workflow.continue_as_new(RegistryParams(state=self._state))

    async def _offer(self, rule: Rule) -> None:
        """Start a rule's one-time offer, then mark it offered (SPEC §14.7 3b).

        The singleton must never die or busy-loop on a failed offer (the gate
        reads its rule table), so a terminal failure is alerted best-effort and
        the rule is marked offered regardless — we move on rather than retry the
        same prompt forever. The id-reuse rejection in ``start_autonomy_offer``
        keeps it one-time even across replay.
        """
        try:
            await workflow.execute_activity(
                offer_activities.start_autonomy_offer,
                rule,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY,
            )
        except ActivityError as exc:
            with contextlib.suppress(ActivityError):
                await workflow.execute_activity(
                    alert_activities.alert_failure,
                    build_failure_alert(
                        key=f"offer-start-{rule.id}",
                        context=(
                            f"starting autonomy offer for "
                            f"{rule.match.payee_pattern}"
                        ),
                        exc=exc,
                    ),
                    start_to_close_timeout=ALERT_TIMEOUT,
                    schedule_to_close_timeout=ALERT_BUDGET,
                    retry_policy=ALERT_RETRY,
                )
        self._state = mark_offered(self._state, rule.id, now=workflow.now())

    def _next_id(self) -> RuleId:
        """A fresh rule id, minted deterministically for replay safety."""
        return RuleId(str(workflow.uuid4()))

    @workflow.signal
    def record(self, feed: FeedRuleLearning) -> None:
        """Fold a W2 confirm/correct effect into the rule table (SPEC §9)."""
        self._state = record_learning(
            self._state, feed, now=workflow.now(), next_id=self._next_id()
        )

    @workflow.signal
    def bless(self, command: ExplicitCommand) -> None:
        """Grant a rule autonomy — the owner's opt-in or standing command."""
        self._state = bless_rule(
            self._state, command, now=workflow.now(), next_id=self._next_id()
        )

    @workflow.signal
    def bless_existing(self, rule_id: str) -> None:
        """Bless an eligible rule by id — the owner accepted its offer (3b).

        Distinct from :meth:`bless`: it grants autonomy to *exactly* the offered
        rule (no match/action reconstruction), and no-ops if that rule is gone
        or no longer eligible, so a stale acceptance can never resurrect it.
        """
        self._state = bless_by_id(
            self._state, RuleId(rule_id), now=workflow.now()
        )

    @workflow.signal
    def revoke(self, payee: str) -> None:
        """Strip autonomy from the payee's blessed rules — "stop" (SPEC §14.5).

        The one-reply undo promise: revoking is the safe direction, so it
        takes effect immediately (no read-back). The rule drops back to
        learned/confirmed and the agent asks again from now on.
        """
        self._state = revoke_payee(self._state, payee, now=workflow.now())

    @workflow.query
    def rules(self) -> tuple[Rule, ...]:
        """The whole rule table (for tests and operators)."""
        return self._state.rules

    @workflow.query
    def view(self) -> RegistryView:
        """The rule table plus the rules eligible for an opt-in bless."""
        return RegistryView(
            rules=self._state.rules,
            eligible=eligible_for_bless(self._state),
        )

    @workflow.query
    def payee_rules(self, payee: str) -> tuple[Rule, ...]:
        """The candidate rules for one payee — the gate's load path."""
        return rules_for_payee(self._state, payee)
