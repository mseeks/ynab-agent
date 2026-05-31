"""W4 · the receipt⇄transaction join — the spine half (SPEC §6).

The *matching* is the model's job: it reasons over amount, date, merchant, and
last-four to decide which transaction a parked receipt belongs to, and says so
as a :data:`MatchOutcome`. This module is the deterministic spine around that
judgment — it never matches; it only enforces the guarantees the model cannot:

  * **Act once.** A receipt signals a given transaction at most once; once
    ``MATCHED`` or ``EXPIRED`` it is terminal and re-checks are no-ops (dedup).
  * **Ask once.** An ambiguous match asks the human a single disambiguation
    question; a later re-check does not re-ask while it stays ambiguous.
  * **Resolve, don't clobber.** Ambiguity becomes a question, never a guess; the
    "don't silently overwrite" rule lives in the W2 the signal lands on.
  * **Age out.** A receipt that never finds a transaction is, after the TTL,
    turned into one "no matching transaction found" note to the sender.

:func:`plan_join` maps ``(receipt, model outcome, now)`` to exactly one
:data:`JoinAction`; :func:`resulting_status` gives the receipt's new store
status. Both are pure — the workflow executes the action and saves the status.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Literal, assert_never

from pydantic import Field

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import ReceiptStatus
from ynab_agent.domain.ids import ReceiptId, YnabTransactionId

if TYPE_CHECKING:
    import datetime

    from ynab_agent.domain.receipt import Receipt

# A parked receipt that never matches is aged out after this (SPEC §6).
DEFAULT_RECEIPT_TTL = timedelta(days=30)
# An ambiguous match means the model found at least this many plausible txns.
_MIN_CANDIDATES = 2


# --- The model's verdict (the agentic half feeds this in) -------------------


class ConfidentMatch(Frozen):
    """The model is confident the receipt belongs to one transaction."""

    kind: Literal["confident"] = "confident"
    txn_id: YnabTransactionId


class Ambiguous(Frozen):
    """The model found several plausible transactions and cannot choose."""

    kind: Literal["ambiguous"] = "ambiguous"
    candidates: tuple[YnabTransactionId, ...] = Field(
        min_length=_MIN_CANDIDATES
    )


class NoMatch(Frozen):
    """The model found no transaction this receipt could belong to (yet)."""

    kind: Literal["no_match"] = "no_match"


MatchOutcome = Annotated[
    ConfidentMatch | Ambiguous | NoMatch, Field(discriminator="kind")
]


# --- The spine's decision (what to actually do) -----------------------------


class SignalTransaction(Frozen):
    """Signal the matched transaction's W2 with this receipt (then MATCHED)."""

    kind: Literal["signal"] = "signal"
    txn_id: YnabTransactionId
    receipt_id: ReceiptId


class AskDisambiguation(Frozen):
    """Ask the human which candidate it is — exactly once (then ASKED)."""

    kind: Literal["ask_disambiguation"] = "ask_disambiguation"
    receipt_id: ReceiptId
    candidates: tuple[YnabTransactionId, ...] = Field(
        min_length=_MIN_CANDIDATES
    )


class AskNoMatch(Frozen):
    """Tell the sender no transaction was found; age the receipt out."""

    kind: Literal["ask_no_match"] = "ask_no_match"
    receipt_id: ReceiptId


class Park(Frozen):
    """Keep waiting: no match yet, still within the TTL. Status unchanged."""

    kind: Literal["park"] = "park"
    reason: str = ""


class DoNothing(Frozen):
    """The receipt is already resolved — dedup. Status unchanged."""

    kind: Literal["do_nothing"] = "do_nothing"
    reason: str = ""


JoinAction = Annotated[
    SignalTransaction | AskDisambiguation | AskNoMatch | Park | DoNothing,
    Field(discriminator="kind"),
]


def plan_join(
    receipt: Receipt,
    outcome: MatchOutcome,
    *,
    now: datetime.datetime,
    ttl: timedelta = DEFAULT_RECEIPT_TTL,
) -> JoinAction:
    """Decide the single join action for a receipt given the model's verdict.

    Args:
        receipt: The parked receipt, carrying its current join status.
        outcome: The model's match verdict for this receipt.
        now: The current time, for TTL expiry (the workflow passes
            ``workflow.now()``).
        ttl: How long a receipt may stay parked before it is aged out.

    Returns:
        Exactly one :data:`JoinAction`. ``MATCHED``/``EXPIRED`` receipts and a
        repeat ambiguous verdict are deduplicated to :class:`DoNothing`/
        :class:`Park`; a confident match always signals, even after an earlier
        ambiguous ask (the human or a new posting may have resolved it).
    """
    # Terminal: the spine already acted on this receipt. Dedup the re-check.
    if receipt.status is ReceiptStatus.MATCHED:
        return DoNothing(reason="already matched")
    if receipt.status is ReceiptStatus.EXPIRED:
        return DoNothing(reason="already aged out")

    match outcome:
        case ConfidentMatch(txn_id=txn_id):
            return SignalTransaction(txn_id=txn_id, receipt_id=receipt.id)
        case Ambiguous(candidates=candidates):
            if receipt.status is ReceiptStatus.ASKED:
                return DoNothing(reason="disambiguation already asked")
            return AskDisambiguation(
                receipt_id=receipt.id, candidates=candidates
            )
        case NoMatch():
            if now - receipt.parked_at > ttl:
                return AskNoMatch(receipt_id=receipt.id)
            return Park(reason="no match yet")
    assert_never(outcome)


def resulting_status(action: JoinAction) -> ReceiptStatus | None:
    """The receipt's new store status after ``action`` (``None`` if unchanged).

    The workflow persists this so the next re-check sees the dedup state.
    """
    match action:
        case SignalTransaction():
            return ReceiptStatus.MATCHED
        case AskDisambiguation():
            return ReceiptStatus.ASKED
        case AskNoMatch():
            return ReceiptStatus.EXPIRED
        case Park() | DoNothing():
            return None
    assert_never(action)
