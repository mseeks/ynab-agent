"""Value types for the W3 dispatch workflow."""

from __future__ import annotations

from pydantic import Field

from ynab_agent.dispatch.classify import InboundMessage
from ynab_agent.domain.base import Frozen


class DispatchParams(Frozen):
    """The W3 dispatch params: the inbound message and sender allow-list."""

    message: InboundMessage
    allowlist: frozenset[str] = Field(default_factory=frozenset)


class DispatchResult(Frozen):
    """What the dispatch did with the message (for observability/audit)."""

    action: str
    detail: str = ""
