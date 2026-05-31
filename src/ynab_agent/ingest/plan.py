"""Ingestion planning: turn a YNAB delta into per-transaction W2 actions.

W1 polls the YNAB ``transactions`` delta and, for each new/unapproved in-scope
transaction, addresses its W2 by ``ynab_id`` via signal-with-start (SPEC §2).
The *decision* of which transactions to address — and how — is pure and lives
here; the spine does the signal-with-start. Two SPEC rules shape it:

* **Cold-start cutover (§13).** On the first run the cursor is captured *without
  acting*, so the agent never emails about the entire pre-existing backlog.
* **Import lifecycle (§13).** A YNAB-matched/duplicate import is not
  auto-approved — it is routed to a human.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.ingest.scope import IngestScope, in_scope

if TYPE_CHECKING:
    from collections.abc import Iterable


class AddressTxn(Frozen):
    """A decision to address one transaction's W2 (start-or-signal).

    Attributes:
        snapshot: The polled YNAB snapshot to hand to the W2.
        route_to_human: ``True`` for a matched/duplicate import — the W2 must
            not auto-approve it (SPEC §13).
    """

    snapshot: YnabSnapshot
    route_to_human: bool = False


def is_duplicate_import(snapshot: YnabSnapshot) -> bool:
    """Whether YNAB matched this import to an existing txn (SPEC §13)."""
    return snapshot.matched_transaction_id is not None


def plan_ingest(
    snapshots: Iterable[YnabSnapshot],
    scope: IngestScope,
    *,
    cold_start: bool,
) -> tuple[AddressTxn, ...]:
    """Plan the W2 actions for one YNAB delta page. Pure.

    Args:
        snapshots: The transactions in this delta page (already normalized).
        scope: The fail-closed ingestion scope.
        cold_start: Whether this is the first poll (capture-cursor-only).

    Returns:
        One :class:`AddressTxn` per in-scope txn, or empty on cold start.
    """
    if cold_start:
        return ()
    return tuple(
        AddressTxn(snapshot=snap, route_to_human=is_duplicate_import(snap))
        for snap in snapshots
        if in_scope(snap, scope)
    )
