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

from temporalio import workflow

from ynab_agent.workflow.registry_types import RegistryParams, RegistryView

with workflow.unsafe.imports_passed_through():
    from ynab_agent.domain.effects import FeedRuleLearning
    from ynab_agent.domain.ids import RuleId
    from ynab_agent.domain.rule import Rule
    from ynab_agent.learn.events import ExplicitCommand
    from ynab_agent.learn.registry import (
        RegistryState,
        bless_rule,
        eligible_for_bless,
        record_learning,
        rules_for_payee,
    )


@workflow.defn
class RuleRegistryWorkflow:
    """The household's one durable rule table (one fold per learning signal)."""

    def __init__(self) -> None:
        """Start empty; the run method adopts any carried-forward state."""
        self._state = RegistryState()

    @workflow.run
    async def run(self, params: RegistryParams) -> None:
        """Hold the rule table, folding signals until history wants rolling."""
        self._state = params.state
        # Park on signals; when Temporal suggests it (history grown), restart
        # fresh carrying the table forward so history stays bounded.
        await workflow.wait_condition(
            lambda: workflow.info().is_continue_as_new_suggested()
        )
        workflow.continue_as_new(RegistryParams(state=self._state))

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
