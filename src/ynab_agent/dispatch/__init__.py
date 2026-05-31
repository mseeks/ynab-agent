"""W3 inbound dispatch: the pure classification/routing core (SPEC §5).

Decides what to do with each inbound AgentMail message, honoring the inbound
boundary (§0.6). The Temporal dispatch workflow
(``ynab_agent.workflow.dispatch_workflow``) executes the decision.
"""

from __future__ import annotations

from ynab_agent.dispatch.classify import (
    DispatchDecision,
    Ignore,
    InboundKind,
    InboundMessage,
    Quarantine,
    RouteToInterpret,
    RouteToTransaction,
    classify,
)

__all__ = [
    "DispatchDecision",
    "Ignore",
    "InboundKind",
    "InboundMessage",
    "Quarantine",
    "RouteToInterpret",
    "RouteToTransaction",
    "classify",
]
