"""The I/O ports of the W4 receipt join, as Temporal activities.

Kept in its own module so the join workflow's sandbox import graph stays
minimal (see ``poll_activities`` / ``dispatch_activities``). The match itself
is the agentic step — but inside a deterministic envelope the model never
crosses:

* the **candidate pool** is prefiltered by exact arithmetic (amount within
  tolerance, date within a window) before the model sees anything;
* a receipt whose total matches **exactly one** candidate (dates agreeing)
  is matched deterministically — no model call at all;
* every id in the model's verdict is validated against the candidates it was
  given (:func:`~ynab_agent.agentic.match.to_match_outcome`), so a
  hallucinated id parks the receipt rather than attaching it.

Heavy clients (YNAB, AgentMail, Temporal, the model stack) are imported
lazily inside the bodies so they never enter a workflow sandbox.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from temporalio import activity

from ynab_agent.domain.enums import ReceiptStatus
from ynab_agent.domain.receipt import Receipt, receipt_summary
from ynab_agent.join.match import ConfidentMatch, MatchOutcome, NoMatch

if TYPE_CHECKING:
    from ynab_agent.domain.transaction import YnabSnapshot

# The deterministic matching envelope. Amounts compare as magnitudes (a
# receipt total is positive, a YNAB outflow negative) in exact milliunits.
_AMOUNT_TOLERANCE_MILLI = 20  # $0.02 — printed-vs-posted rounding
_EXACT_MILLI = 5  # to-the-cent agreement for the no-model shortcut
_DATE_WINDOW = datetime.timedelta(days=3)  # plausible post-date drift
_SHORTCUT_DATE_WINDOW = datetime.timedelta(days=1)  # SPEC §6: ±1 day
_DATED_LOOKBACK = datetime.timedelta(days=7)  # pool starts before the receipt
_UNDATED_LOOKBACK = datetime.timedelta(days=45)  # no receipt date: recent-ish
_MAX_CANDIDATES = 12


def _magnitude_gap(snapshot: YnabSnapshot, receipt: Receipt) -> int | None:
    """|txn| - |total| in milliunits, or ``None`` without a receipt total."""
    if receipt.total is None:
        return None
    return abs(abs(snapshot.amount.milliunits) - receipt.total.milliunits)


def _date_gap(snapshot: YnabSnapshot, receipt: Receipt) -> int | None:
    """Days between posting and the receipt, or ``None`` without a date."""
    if receipt.date is None:
        return None
    return abs((snapshot.txn_date - receipt.date).days)


def _since(receipt: Receipt, today: datetime.date) -> datetime.date:
    """Where the candidate read starts: just before the receipt's date."""
    if receipt.date is not None:
        return receipt.date - _DATED_LOOKBACK
    return today - _UNDATED_LOOKBACK


def candidate_pool(
    receipt: Receipt, transactions: tuple[YnabSnapshot, ...]
) -> tuple[YnabSnapshot, ...]:
    """The transactions plausibly belonging to this receipt. Pure.

    Amount agreement (within tolerance) or date proximity admits a candidate;
    a receipt with neither a total nor a date (merchant-only) gets the whole
    window and the model matches on the payee. Sorted by amount gap then date
    gap, capped — the model only ever reasons over a short, plausible list.
    """

    def admit(snapshot: YnabSnapshot) -> bool:
        # A purchase receipt belongs to an outflow, never an inflow: a
        # same-magnitude refund must not match (the exact shortcut would
        # otherwise attach the receipt to it with no model in the loop).
        # Transfers are spending nowhere — never receipt-able either.
        if snapshot.amount.milliunits >= 0:
            return False
        if snapshot.payee.startswith("Transfer"):
            return False
        amount_gap = _magnitude_gap(snapshot, receipt)
        date_gap = _date_gap(snapshot, receipt)
        if amount_gap is None and date_gap is None:
            return True  # merchant-only: the window is the filter
        if amount_gap is not None and amount_gap <= _AMOUNT_TOLERANCE_MILLI:
            return True
        return date_gap is not None and date_gap <= _DATE_WINDOW.days

    def rank(snapshot: YnabSnapshot) -> tuple[int, int]:
        amount_gap = _magnitude_gap(snapshot, receipt)
        date_gap = _date_gap(snapshot, receipt)
        return (
            amount_gap if amount_gap is not None else 10**9,
            date_gap if date_gap is not None else 10**9,
        )

    admitted = sorted((t for t in transactions if admit(t)), key=rank)
    return tuple(admitted[:_MAX_CANDIDATES])


def exact_single_match(
    receipt: Receipt, pool: tuple[YnabSnapshot, ...]
) -> ConfidentMatch | None:
    """The no-model shortcut: one to-the-cent candidate, dates agreeing.

    Exactly one transaction in the pool matching the receipt's total to the
    cent — with the dates within a day when both are known — is a match by
    arithmetic, not judgment. More than one exact candidate (the two-coffees
    case SPEC §6 calls out) falls through to the model / a question.
    """
    if receipt.total is None:
        return None
    exact = [
        t
        for t in pool
        if (gap := _magnitude_gap(t, receipt)) is not None
        and gap <= _EXACT_MILLI
        and (
            (days := _date_gap(t, receipt)) is None
            or days <= _SHORTCUT_DATE_WINDOW.days
        )
    ]
    if len(exact) == 1:
        return ConfidentMatch(txn_id=exact[0].ynab_id)
    return None


@activity.defn
async def match_receipt(receipt: Receipt) -> MatchOutcome:
    """Match a receipt against recent transactions (SPEC §6).

    Deterministic envelope first (window read, plausibility pool, the exact
    shortcut); the model only arbitrates a genuinely fuzzy pool, and its
    verdict is validated against the candidates it was given.
    """
    import asyncio

    from ynab_agent.agentic.match import (
        CandidateTxn,
        MatchRequest,
        ReceiptFacts,
        to_match_outcome,
    )
    from ynab_agent.agentic.match import match_receipt as run_match_agent
    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    today = datetime.datetime.now(datetime.UTC).date()
    transactions = await asyncio.to_thread(
        client.recent, _since(receipt, today)
    )
    pool = candidate_pool(receipt, transactions)
    if not pool:
        return NoMatch()
    shortcut = exact_single_match(receipt, pool)
    if shortcut is not None:
        return shortcut

    request = MatchRequest(
        receipt=ReceiptFacts(
            merchant=receipt.merchant or "(not stated)",
            total_display=(
                str(receipt.total)
                if receipt.total is not None
                else "(not stated)"
            ),
            date_display=(receipt.date.isoformat() if receipt.date else None),
        ),
        candidates=tuple(
            CandidateTxn(
                id=str(t.ynab_id),
                payee=t.payee,
                amount_display=str(t.amount),
                date_display=t.txn_date.isoformat(),
            )
            for t in pool
        ),
    )
    verdict = await run_match_agent(request)
    return to_match_outcome(verdict, request)


async def _ledger_get(receipt_id: str) -> Receipt | None:
    """The receipt by id from the durable ledger, or ``None``.

    The optional result is hydrated manually (the SDK cannot type a
    ``Receipt | None`` query result); a missing ledger reads as missing.
    """
    from temporalio.service import RPCError

    from ynab_agent.workflow.receipt_ledger_types import (
        RECEIPT_LEDGER_WORKFLOW_ID,
    )
    from ynab_agent.workflow.temporal_client import client

    temporal = await client()
    handle = temporal.get_workflow_handle(RECEIPT_LEDGER_WORKFLOW_ID)
    try:
        raw = await handle.query("get", receipt_id)
    except RPCError:
        return None
    if raw is None:
        return None
    return Receipt.model_validate(raw)


async def _fold_memo_directly(txn_id: str, receipt: Receipt) -> None:
    """Fold the receipt into a settled charge's memo — memo-only (SPEC §6).

    For a transaction with no live W2 (hand-approved in the app, pre-install,
    or long archived) there is nothing to re-triage and nothing that may be
    re-decided: ``plan_ingest``'s invariant — the agent never re-opens a
    settled charge — holds here too. So the fold is a partial PATCH of the
    memo alone (category, approval, and flag untouched), verified by
    re-read, and confirmed on the *receipt's* own thread (the charge has no
    conversation to speak on). Failures raise before MATCHED is saved, so a
    later re-check retries cleanly.
    """
    import asyncio

    from ynab_agent.agentic.compose import (
        render_receipt_matched,
        render_receipt_matched_html,
    )
    from ynab_agent.domain.receipt import receipt_memo
    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings
    from ynab_agent.ynab.client import YnabClient

    ynab = YnabClient.from_env()
    snapshot = await asyncio.to_thread(ynab.snapshot, txn_id)
    if snapshot is None:
        msg = f"transaction {txn_id} unreadable for the receipt fold"
        raise RuntimeError(msg)
    merged = receipt_memo(receipt, snapshot.memo)
    if merged != (snapshot.memo or "").strip():
        await asyncio.to_thread(ynab.patch_memo, txn_id, merged)
        read = await asyncio.to_thread(ynab.snapshot, txn_id)
        if read is None or (read.memo or "").strip() != merged:
            msg = f"memo fold on {txn_id} did not verify"
            raise RuntimeError(msg)
    if receipt.source_thread_id is None:
        return
    date = snapshot.txn_date.strftime("%b ") + str(snapshot.txn_date.day)
    charge = f"{snapshot.payee} — {snapshot.amount} on {date}"
    settings = Settings()
    mail = MailClient.from_env()
    await asyncio.to_thread(
        mail.send_on_thread,
        inbox_id=settings.inbox,
        thread_id=str(receipt.source_thread_id),
        body=render_receipt_matched(receipt_summary(receipt), charge),
        seq_label=f"yarcpt-matched-{receipt.id}",
        to=list(settings.owners),
        html=render_receipt_matched_html(receipt, charge),
    )


@activity.defn
async def signal_match(txn_id: str, receipt_id: str) -> None:
    """Deliver the matched receipt to its transaction (SPEC §6).

    A RUNNING W2 owns the transaction's conversation and writes, so the
    receipt is signaled to it (carrying the parsed receipt itself — W2's
    consumers need no ledger read). With no live W2 — a charge the owner
    hand-approved before the poll, a pre-install or out-of-scope one, or a
    long-archived run — starting one would re-triage a settled charge (a
    fresh proposal email, or a blessed rule rewriting it), so the memo is
    folded directly instead. Raises when the ledger does not know the
    receipt — the join attempt fails *before* MATCHED is saved, so a later
    re-check retries cleanly.
    """
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError

    from ynab_agent.domain.ids import ReceiptId
    from ynab_agent.domain.signals import ReceiptSignal
    from ynab_agent.workflow.temporal_client import client

    receipt = await _ledger_get(receipt_id)
    if receipt is None:
        msg = f"receipt {receipt_id} is not in the ledger"
        raise RuntimeError(msg)
    temporal = await client()
    handle = temporal.get_workflow_handle(txn_id)
    try:
        description = await handle.describe()
        running = description.status is WorkflowExecutionStatus.RUNNING
    except RPCError:
        running = False
    if running:
        await handle.signal(
            "submit_inbound",
            ReceiptSignal(receipt_id=ReceiptId(receipt_id), receipt=receipt),
        )
        return
    await _fold_memo_directly(txn_id, receipt)


async def _candidate_lines(
    candidates: list[str], inbox: str
) -> tuple[list[str], bool]:
    """Human one-liners for the disambiguation options, amount first.

    Returns the lines and whether ANY candidate has its own email thread —
    the instruction must only promise a thread that exists (a hand-approved
    or pre-install charge was never triaged and has none).
    """
    import asyncio
    import functools

    from ynab_agent.mail.client import MailClient
    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    mail = MailClient.from_env()
    lines: list[str] = []
    any_thread = False
    for txn_id in candidates[:4]:
        snapshot = await asyncio.to_thread(client.snapshot, txn_id)
        if snapshot is None:
            continue
        probe = functools.partial(
            mail.has_thread, inbox_id=inbox, label=f"yatxn-{txn_id}"
        )
        any_thread = any_thread or await asyncio.to_thread(probe)
        date = snapshot.txn_date.strftime("%b ") + str(snapshot.txn_date.day)
        # Amount first: in the canonical near-duplicates case the payee is
        # identical, so the amount + date ARE the decision.
        lines.append(f"{snapshot.amount} at {snapshot.payee} on {date}")
    return lines, any_thread


@activity.defn
async def ask_disambiguation(receipt_id: str, candidates: list[str]) -> None:
    """Ask the sender which candidate the receipt belongs to — once (§6).

    Replies on the receipt's own thread, deduped on the receipt id, naming
    each plausible charge — and the instruction only points at the charges'
    own email threads when at least one exists. Always either sends or
    raises: the workflow saves ASKED after this, and a silent no-send would
    mark a question asked that no one ever received.
    """
    import asyncio

    from ynab_agent.agentic.compose import (
        render_receipt_disambiguation,
        render_receipt_disambiguation_html,
    )
    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings

    receipt = await _ledger_get(receipt_id)
    if receipt is None or receipt.source_thread_id is None:
        msg = f"receipt {receipt_id} has no thread to ask on"
        raise RuntimeError(msg)
    settings = Settings()
    options, any_thread = await _candidate_lines(candidates, settings.inbox)
    if not options:
        msg = f"no candidate of receipt {receipt_id} is readable in YNAB"
        raise RuntimeError(msg)
    mail = MailClient.from_env()
    await asyncio.to_thread(
        mail.send_on_thread,
        inbox_id=settings.inbox,
        thread_id=str(receipt.source_thread_id),
        body=render_receipt_disambiguation(
            receipt_summary(receipt), tuple(options), with_threads=any_thread
        ),
        seq_label=f"yarcpt-ask-{receipt_id}",
        to=list(settings.owners),
        html=render_receipt_disambiguation_html(
            receipt, tuple(options), with_threads=any_thread
        ),
    )


@activity.defn
async def ask_no_match(receipt_id: str) -> None:
    """Tell the sender no matching transaction was found (TTL expiry, §6)."""
    import asyncio

    from ynab_agent.agentic.compose import (
        render_receipt_no_match,
        render_receipt_no_match_html,
    )
    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings

    receipt = await _ledger_get(receipt_id)
    if receipt is None or receipt.source_thread_id is None:
        return
    settings = Settings()
    mail = MailClient.from_env()
    await asyncio.to_thread(
        mail.send_on_thread,
        inbox_id=settings.inbox,
        thread_id=str(receipt.source_thread_id),
        body=render_receipt_no_match(receipt_summary(receipt)),
        seq_label=f"yarcpt-nomatch-{receipt_id}",
        to=list(settings.owners),
        html=render_receipt_no_match_html(receipt),
    )


@activity.defn
async def save_receipt_status(receipt_id: str, status: ReceiptStatus) -> None:
    """Persist the receipt's new join status so re-checks dedup (SPEC §6).

    Signal-with-start on the singleton ledger, the same shape every other
    ledger uses (``record_auto_action``, ``save_alert``).
    """
    from temporalio.common import WorkflowIDConflictPolicy

    from ynab_agent.workflow.receipt_ledger_types import (
        RECEIPT_LEDGER_WORKFLOW_ID,
        ReceiptLedgerParams,
        SetStatusRequest,
    )
    from ynab_agent.workflow.temporal_client import client, task_queue

    temporal = await client()
    await temporal.start_workflow(
        "ReceiptLedgerWorkflow",
        ReceiptLedgerParams(),
        id=RECEIPT_LEDGER_WORKFLOW_ID,
        task_queue=task_queue(),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="set_status",
        start_signal_args=[
            SetStatusRequest(receipt_id=receipt_id, status=status)
        ],
    )


async def park_in_ledger(receipt: Receipt) -> None:
    """Add a parsed receipt to the durable ledger (idempotent on its id).

    A plain helper (not an activity) so ``route_receipt`` — itself an
    activity — can call it directly.
    """
    from temporalio.common import WorkflowIDConflictPolicy

    from ynab_agent.workflow.receipt_ledger_types import (
        RECEIPT_LEDGER_WORKFLOW_ID,
        ReceiptLedgerParams,
    )
    from ynab_agent.workflow.temporal_client import client, task_queue

    temporal = await client()
    await temporal.start_workflow(
        "ReceiptLedgerWorkflow",
        ReceiptLedgerParams(),
        id=RECEIPT_LEDGER_WORKFLOW_ID,
        task_queue=task_queue(),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="park",
        start_signal_args=[receipt],
    )


@activity.defn
async def list_open_receipts() -> tuple[Receipt, ...]:
    """The parked/asked receipts a W1 re-check should attempt (SPEC §6).

    An absent ledger (no receipt ever forwarded) reads as empty, never an
    error — the poll tick must not page on a feature that was simply never
    used.
    """
    from temporalio.service import RPCError

    from ynab_agent.workflow.receipt_ledger_types import (
        RECEIPT_LEDGER_WORKFLOW_ID,
    )
    from ynab_agent.workflow.temporal_client import client

    temporal = await client()
    handle = temporal.get_workflow_handle(RECEIPT_LEDGER_WORKFLOW_ID)
    try:
        receipts: tuple[Receipt, ...] = await handle.query(
            "open_receipts", result_type=tuple[Receipt, ...]
        )
    except RPCError:
        return ()
    return receipts


async def start_join(receipt: Receipt) -> None:
    """Start one join attempt for a receipt (a plain helper, see above).

    Each attempt is its own short execution on the same id; an attempt still
    running covers the tick, so an already-started error is a no-op. Dedup of
    *effects* lives in the ledger status (``plan_join``) and the mail labels,
    not here.
    """
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from ynab_agent.workflow.receipt_types import ReceiptJoinParams
    from ynab_agent.workflow.temporal_client import client, task_queue

    temporal = await client()
    try:
        await temporal.start_workflow(
            "ReceiptJoinWorkflow",
            ReceiptJoinParams(receipt=receipt),
            id=f"receipt-join-{receipt.id}",
            task_queue=task_queue(),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        return


@activity.defn
async def start_receipt_join(receipt: Receipt) -> None:
    """Start one join attempt for a receipt (W1's parked re-check, SPEC §6)."""
    await start_join(receipt)
