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
    """The W1 poll's params: the scope and the prior cursor.

    ``cursor`` is ``None`` on the very first poll, which triggers the cold-start
    cutover (capture the cursor without acting; SPEC §13).
    """

    scope: IngestScope
    cursor: int | None = None


class PollResult(Frozen):
    """The outcome of one poll run."""

    addressed: int
    routed_to_human: int
    new_cursor: int
