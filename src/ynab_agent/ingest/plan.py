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
        notify_existing: ``True`` when an Amazon item-detail memo has backfilled
            (Amazon payee + a memo now present). If the W2 is already in flight,
            W1 signals it the fresh snapshot so a ``HOLD_AMAZON`` run resolves
            early instead of waiting out the ~36h deadline (SPEC §2, §3).
    """

    snapshot: YnabSnapshot
    route_to_human: bool = False
    notify_existing: bool = False


def is_duplicate_import(snapshot: YnabSnapshot) -> bool:
    """Whether YNAB matched this import to an existing txn (SPEC §13).

    Delegates to the snapshot's own predicate so W1's observability flag and the
    gate's auto-apply guard read the *same* definition (no drift).
    """
    return snapshot.is_matched_import


def is_amazon(payee: str) -> bool:
    """Whether a payee is Amazon-ish, so the §3 item-detail hold applies.

    A deliberately loose substring match (SPEC §11 leaves the exact payee
    patterns open). The *same* predicate gates W1's backfill signal here and
    W2's hold entry, so the two never disagree about what counts as Amazon.
    """
    return "amazon" in payee.lower()


def memo_backfilled(snapshot: YnabSnapshot) -> bool:
    """Whether an Amazon hold can now resolve: Amazon payee + memo present.

    The condition W1 turns into a ``notify_snapshot`` to an already-running W2
    (SPEC §2, §3). W1 is stateless per tick, so it cannot see the empty→present
    *transition*; an Amazon txn that currently carries a memo is exactly the
    resolvable case, and signalling a W2 that is already past the hold is a
    harmless no-op.
    """
    return is_amazon(snapshot.payee) and snapshot.has_memo


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
        One :class:`AddressTxn` per in-scope, unapproved txn — empty on cold
        start.

    Only *unapproved* transactions are addressed (SPEC §2/§13 "new/unapproved").
    An approved transaction is the owner's settled state — the agent never
    re-triages or emails about something already approved (whether the owner
    approved it in YNAB or the agent's own triage did). This is what makes "act
    on the current backlog" safe: only the outstanding, unapproved transactions
    surface.
    """
    if cold_start:
        return ()
    return tuple(
        AddressTxn(
            snapshot=snap,
            route_to_human=is_duplicate_import(snap),
            notify_existing=memo_backfilled(snap),
        )
        for snap in snapshots
        if in_scope(snap, scope) and not snap.approved
    )
