"""Value types for the W1 poll workflow."""

from __future__ import annotations

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.ingest.scope import IngestScope


class DeltaPage(Frozen):
    """One page of the YNAB transactions delta and its cursor."""

    snapshots: tuple[YnabSnapshot, ...] = ()
    server_knowledge: int = 0


class PollParams(Frozen):
    """The W1 poll's params: scope, prior cursor, and the loop knobs.

    ``cursor`` is ``None`` on the very first poll, which triggers the cold-start
    cutover (capture the cursor without acting; SPEC §13). When ``continuous``
    is set the workflow is a durable loop: after each tick it sleeps
    ``interval_seconds`` and continues-as-new carrying the advanced cursor in
    workflow state (store-free, SPEC §0.5). A one-shot run (the default, and
    what tests use) returns its :class:`PollResult` instead of looping.
    """

    scope: IngestScope
    cursor: int | None = None
    interval_seconds: int = 3600
    continuous: bool = False


class PollResult(Frozen):
    """The outcome of one poll run."""

    addressed: int
    routed_to_human: int
    new_cursor: int
