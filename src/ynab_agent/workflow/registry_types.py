"""Params and query views for the durable rule registry (W5, SPEC §14).

Kept apart from :mod:`ynab_agent.workflow.registry_workflow` so the activities
and the gate can import the param/view shapes without dragging the workflow
definition (and its sandbox import rules) along.
"""

from __future__ import annotations

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.rule import Rule
from ynab_agent.learn.registry import RegistryState

# The singleton workflow id: there is exactly one rule registry per deployment,
# born on the first learning signal and living forever via continue-as-new.
REGISTRY_WORKFLOW_ID = "ynab-rule-registry"


class RegistryParams(Frozen):
    """The registry's run input — the carried state across continue-as-new."""

    state: RegistryState = RegistryState()


class RegistryView(Frozen):
    """A read of the registry for callers (the gate, the on-ramp prompt)."""

    rules: tuple[Rule, ...] = ()
    eligible: tuple[Rule, ...] = ()
