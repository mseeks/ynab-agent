"""Value types for the W1 poll workflow."""

from __future__ import annotations

from ynab_agent.domain.base import Frozen
from ynab_agent.ingest.scope import IngestScope


class PollParams(Frozen):
    """The W1 poll's params: the scope and the loop knobs.

    The poll re-reads YNAB's *unapproved* set each tick (the outstanding work),
    so there is no cursor to carry — "what's outstanding" is derived from YNAB,
    not stored (SPEC §0.5). When ``continuous`` is set the workflow is a durable
    loop: after each tick it sleeps ``interval_seconds`` and continues-as-new. A
    one-shot run (the default, and what tests use) returns its result instead.
    """

    scope: IngestScope
    interval_seconds: int = 3600
    continuous: bool = False


class PollResult(Frozen):
    """The outcome of one poll run."""

    addressed: int
    routed_to_human: int
