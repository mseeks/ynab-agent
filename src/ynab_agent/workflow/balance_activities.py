"""The I/O ports of the W7 budget balancer, as Temporal activities (SPEC §8).

Its own module so the balance workflow's sandbox import graph stays minimal (see
``offer_activities``). Heavy clients (Temporal, YNAB, AgentMail, the model) are
imported lazily inside the bodies so they never enter a workflow sandbox.

Division of labor: these activities read YNAB, run the model, and write one
category's ``budgeted`` (idempotent absolute set + read-back verify); the
*workflow* computes absolute targets from a baseline snapshot and orchestrates
the writes, so a write retry re-sets the same value and never double-applies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from temporalio import activity

from ynab_agent.budget.balance import (
    BalanceOffer,
    BalanceOption,
    BalanceOutcome,
    BudgetMove,
    SourceView,
)
from ynab_agent.budget.overspend import MonthClock, OverspendAssessment
from ynab_agent.domain.money import Money
from ynab_agent.workflow.balance_types import BalanceParams, BudgetState

if TYPE_CHECKING:
    # ``SourceFunds`` lives in ``agentic.balance``, which pulls in pydantic-ai —
    # never import it at module scope here (this module is passed through the
    # workflow sandbox). The runtime import is lazy, inside the bodies.
    from ynab_agent.agentic.balance import SourceFunds
    from ynab_agent.budget.balance import Source


def _dollars(amount: Money) -> float:
    """A Money amount as a float dollar figure for the model's context."""
    return float(amount.currency_amount)


def _overspend_note(assessment: OverspendAssessment) -> str:
    """A one-line human summary of how the category is tracking."""
    return (
        f"{assessment.name}: {assessment.spent} spent of "
        f"{assessment.budgeted} budgeted, projected ~{assessment.projected} "
        "by month-end."
    )


def _source_name(category: str, names: dict[str, str], *, is_rta: bool) -> str:
    """The donor's display name (Ready to Assign for the sentinel)."""
    return "Ready to Assign" if is_rta else names.get(category, category)


def _to_source_funds(
    sources: tuple[Source, ...], names: dict[str, str]
) -> tuple[SourceFunds, ...]:
    """Domain sources as the model's ``SourceFunds`` (slack + names)."""
    from ynab_agent.agentic.balance import SourceFunds
    from ynab_agent.budget.balance import READY_TO_ASSIGN_SOURCE

    funds: list[SourceFunds] = []
    for source in sources:
        is_rta = source.category == READY_TO_ASSIGN_SOURCE
        funds.append(
            SourceFunds(
                id=str(source.category),
                name=_source_name(str(source.category), names, is_rta=is_rta),
                available=_dollars(source.available),
                kind="ready-to-assign" if is_rta else "category",
                slack=_dollars(source.drawable),
                projection=_dollars(source.projection),
            )
        )
    return tuple(funds)


def _to_source_views(
    sources: tuple[Source, ...], names: dict[str, str]
) -> tuple[SourceView, ...]:
    """Donor views (name + slack) for rendering the offer's real numbers."""
    from ynab_agent.budget.balance import READY_TO_ASSIGN_SOURCE

    return tuple(
        SourceView(
            category=source.category,
            name=_source_name(
                str(source.category),
                names,
                is_rta=source.category == READY_TO_ASSIGN_SOURCE,
            ),
            slack=source.drawable,
        )
        for source in sources
    )


def _now_clock() -> MonthClock:
    """The current month position, in household time (SPEC §13).

    Computed at the activity's own time so a donor's slack reflects where the
    month is *now* — the offer is proposed and the reply read on different days.
    """
    import datetime

    from ynab_agent.budget.overspend import period_and_clock

    _, clock = period_and_clock(datetime.datetime.now(datetime.UTC))
    return clock


@activity.defn
async def start_balance_offer(
    assessment: OverspendAssessment, thread_id: str, period: str
) -> None:
    """Start the balance offer for an alerted category (SPEC §8, the W6→W7 tie).

    Started ``REJECT_DUPLICATE`` on the (category, period) id, so a worsening
    re-alert in the same month is a no-op, not a second offer; at most one
    coverage offer per category per period, matching the monitor's own dedupe.
    ``period`` is supplied by the workflow (the same value the alert thread was
    keyed on), so the offer id and the alert thread can never drift apart.
    """
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from ynab_agent.budget.balance import need_from_assessment
    from ynab_agent.workflow.balance_types import (
        BalanceParams,
        balance_workflow_id,
    )
    from ynab_agent.workflow.temporal_client import client, task_queue

    if need_from_assessment(assessment).shortfall.is_zero:
        return  # nothing to cover
    temporal = await client()
    try:
        await temporal.start_workflow(
            "BudgetBalanceWorkflow",
            BalanceParams(
                assessment=assessment, thread_id=thread_id, period=period
            ),
            id=balance_workflow_id(str(assessment.category), period),
            task_queue=task_queue(),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        return


@activity.defn
async def propose_balance_options(params: BalanceParams) -> BalanceOffer:
    """Read the budget, ask the model for options, keep the feasible ones (§8).

    Donors are now selected by *slack* (what each can spare after its own
    projected spend), so a category heading over itself is never offered. The
    model proposes; the deterministic guard (:func:`feasible_options`) drops
    anything that doesn't add up, overdraws a source, or pulls a donor below its
    slack. When the model yields nothing usable, fall back to the greedy plan;
    empty ``options`` means even that can't cover it from current funds. The
    returned ``sources`` carry each donor's name + slack so the offer renders
    real numbers.
    """
    import asyncio

    from ynab_agent.agentic.balance import (
        BalanceContext,
        propose_balance,
        to_options,
    )
    from ynab_agent.budget.balance import (
        fallback_option,
        feasible_options,
        need_from_assessment,
        sources_from_spends,
    )
    from ynab_agent.ynab.client import YnabClient

    assessment = params.assessment
    need = need_from_assessment(assessment)
    if need.shortfall.is_zero:
        return BalanceOffer(options=(), sources=())
    client = YnabClient.from_env()
    spends = await asyncio.to_thread(client.category_spends)
    rta = await asyncio.to_thread(client.ready_to_assign)
    sources = sources_from_spends(
        spends, rta, exclude=assessment.category, clock=_now_clock()
    )
    if not sources:
        return BalanceOffer(options=(), sources=())
    names = {str(spend.category): spend.name for spend in spends}
    views = _to_source_views(sources, names)
    context = BalanceContext(
        needy_category_id=str(assessment.category),
        needy_category_name=assessment.name,
        shortfall=_dollars(need.shortfall),
        overspend_note=_overspend_note(assessment),
        sources=_to_source_funds(sources, names),
    )
    proposal = await propose_balance(context)
    options = to_options(proposal, destination=assessment.category)
    feasible = feasible_options(options, need, sources)
    if feasible:
        return BalanceOffer(options=tuple(feasible), sources=views)
    fallback = fallback_option(need, sources)
    chosen = (fallback,) if fallback is not None else ()
    return BalanceOffer(options=chosen, sources=views)


@activity.defn
async def interpret_balance_reply(
    params: BalanceParams, reply_text: str, options: list[BalanceOption]
) -> BalanceOutcome:
    """Read the owner's free-text reply into a concrete outcome (SPEC §8).

    Re-reads the sources (funds may have shifted since the offer) so the model
    resolves "from dining instead" against current reality; the ``to_*`` seam
    maps the reading onto a domain outcome the workflow branches on.
    """
    import asyncio

    from ynab_agent.agentic.balance import (
        BalanceReplyRequest,
        MoveSpec,
        OfferedOption,
        to_balance_outcome,
    )
    from ynab_agent.agentic.balance import (
        interpret_balance_reply as run_reply,
    )
    from ynab_agent.budget.balance import (
        need_from_assessment,
        sources_from_spends,
    )
    from ynab_agent.ynab.client import YnabClient

    assessment = params.assessment
    client = YnabClient.from_env()
    spends = await asyncio.to_thread(client.category_spends)
    rta = await asyncio.to_thread(client.ready_to_assign)
    sources = sources_from_spends(
        spends, rta, exclude=assessment.category, clock=_now_clock()
    )
    names = {str(spend.category): spend.name for spend in spends}
    offered = tuple(
        OfferedOption(
            label=option.label,
            moves=tuple(
                MoveSpec(
                    source_category_id=str(move.source),
                    amount=_dollars(move.amount),
                )
                for move in option.moves
            ),
            rationale=option.rationale,
        )
        for option in options
    )
    request = BalanceReplyRequest(
        reply_text=reply_text,
        needy_category_name=assessment.name,
        shortfall=_dollars(need_from_assessment(assessment).shortfall),
        options=offered,
        sources=_to_source_funds(sources, names),
    )
    reading = await run_reply(request)
    return to_balance_outcome(reading, destination=assessment.category)


@activity.defn
async def read_budget_state() -> BudgetState:
    """Snapshot funds, slack, and budgets for the apply (SPEC §8).

    ``slack`` re-derives each donor's protected drawable at apply time (funds
    may have shifted since the offer), so the apply-time guard refuses a move
    that would now pull a category below its own projected spend.
    """
    import asyncio

    from ynab_agent.budget.balance import (
        READY_TO_ASSIGN_SOURCE,
        donor_slack,
    )
    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    spends = await asyncio.to_thread(client.category_spends)
    rta = await asyncio.to_thread(client.ready_to_assign)
    clock = _now_clock()
    available = {spend.category: spend.balance for spend in spends}
    available[READY_TO_ASSIGN_SOURCE] = rta
    slack = {spend.category: donor_slack(spend, clock)[0] for spend in spends}
    slack[READY_TO_ASSIGN_SOURCE] = rta
    budgeted = {spend.category: spend.budgeted for spend in spends}
    return BudgetState(available=available, budgeted=budgeted, slack=slack)


@activity.defn
async def set_category_budgeted(category_id: str, target: Money) -> bool:
    """Set a category's month budget to ``target`` and verify it (SPEC §8).

    An absolute write (idempotent on retry), then an independent read-back —
    never trusting its echo (SPEC §0.6). ``True`` when the read confirms.
    """
    import asyncio

    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    await asyncio.to_thread(client.set_budgeted, category_id, target)
    actual = await asyncio.to_thread(client.read_budgeted, category_id)
    return actual == target


@activity.defn
async def log_budget_moves(moves: list[BudgetMove], period: str) -> None:
    """Record each applied reallocation to the audit trail (SPEC §8, §9)."""
    from ynab_agent.audit.record import record_budget_move

    for move in moves:
        event = record_budget_move(move, period)
        activity.logger.info(
            "budget move applied", extra={"audit": event.model_dump()}
        )


@activity.defn
async def send_balance_email(thread_id: str, body: str, seq_label: str) -> None:
    """Reply on the overspend-alert thread, addressed to the owners (SPEC §8).

    The whole balance conversation lives on the W6 alert thread (the W6→W7 tie):
    options, clarifications, and the apply/decline confirmation all reply there,
    so the owner sees one thread per overspend. ``to`` is set explicitly because
    that thread's latest message is often the agent's own (the alert, or a
    back-to-back agent reply); without it AgentMail addresses the reply back to
    the agent and the owner never receives it. Idempotent on ``seq_label``.
    """
    import asyncio

    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings

    settings = Settings()
    mail = MailClient.from_env()
    await asyncio.to_thread(
        mail.send_on_thread,
        inbox_id=settings.inbox,
        thread_id=thread_id,
        body=body,
        seq_label=seq_label,
        to=list(settings.owners),
    )
