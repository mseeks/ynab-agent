"""The I/O ports of the W7 coordinated budget balancer, as activities (SPEC §8).

Its own module so the balance workflow's sandbox import graph stays minimal (see
``offer_activities``). Heavy clients (Temporal, YNAB, AgentMail, the model) are
imported lazily inside the bodies so they never enter a workflow sandbox.

One coordinated plan per monitor pass over one shared, slack-ranked donor pool
(#46): two needs can never both drain the same donor, so a double-drain is
impossible by construction. The plan is the deterministic greedy coverage; the
owner approves the whole plan ("do it") or declines. The apply writes each
category's *absolute* target ``budgeted`` (idempotent set + read-back verify);
the workflow computes targets from a baseline snapshot, so a retry never
double-applies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from temporalio import activity

from ynab_agent.budget.balance import (
    BudgetMove,
    CoordinatedOffer,
    CoverageLine,
    SourceView,
)
from ynab_agent.budget.overspend import MonthClock, OverspendAssessment
from ynab_agent.domain.money import Money
from ynab_agent.workflow.balance_types import (
    BalanceParams,
    BudgetState,
    CoordinatedReplyResult,
)

if TYPE_CHECKING:
    from ynab_agent.budget.balance import Source


def _source_name(category: str, names: dict[str, str], *, is_rta: bool) -> str:
    """The donor's display name (Ready to Assign for the sentinel)."""
    return "Ready to Assign" if is_rta else names.get(category, category)


def _to_source_views(
    sources: tuple[Source, ...], names: dict[str, str]
) -> tuple[SourceView, ...]:
    """Donor views (name + slack) for rendering the plan's real numbers."""
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
async def start_coordinated_balance(
    assessments: list[OverspendAssessment], period: str
) -> None:
    """Start the one coordinated coverage offer for a pass (SPEC §8, #46).

    REJECT_DUPLICATE on the per-period id, so the daily monitor starts at most
    one coordinated balancer per budget month; a later same-period pass is a
    no-op. Skips entirely when no category has a real shortfall.
    """
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from ynab_agent.budget.balance import needs_from_assessments
    from ynab_agent.workflow.balance_types import balance_workflow_id
    from ynab_agent.workflow.temporal_client import client, task_queue

    if not needs_from_assessments(assessments):
        return  # nothing to cover
    temporal = await client()
    try:
        await temporal.start_workflow(
            "CoordinatedBalanceWorkflow",
            BalanceParams(assessments=tuple(assessments), period=period),
            id=balance_workflow_id(period),
            task_queue=task_queue(),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        return


@activity.defn
async def propose_coordinated_offer(params: BalanceParams) -> CoordinatedOffer:
    """One coordinated plan over one shared, slack-ranked pool (SPEC §8, #46).

    Reads the budget once, builds the shared donor pool (excluding *every* needy
    category, so no donor is double-claimed), and greedily covers all needs from
    it — biggest gap first. The returned offer carries the plan to apply, the
    named lines to render, the donor slacks for the "leaves" summary, and any
    category the pool couldn't reach. Empty ``moves`` means nothing safe covers
    anything.
    """
    import asyncio

    from ynab_agent.budget.balance import (
        READY_TO_ASSIGN_SOURCE,
        needs_from_assessments,
        plan_coverage,
        sources_from_spends,
    )
    from ynab_agent.ynab.client import YnabClient

    needs = needs_from_assessments(params.assessments)
    if not needs:
        return CoordinatedOffer(moves=(), lines=(), sources=())
    client = YnabClient.from_env()
    spends = await asyncio.to_thread(client.category_spends)
    rta = await asyncio.to_thread(client.ready_to_assign)
    exclude = frozenset(need.category for need in needs)
    sources = sources_from_spends(
        spends, rta, exclude=exclude, clock=_now_clock()
    )
    plan = plan_coverage(needs, list(sources))
    if not plan.moves:
        return CoordinatedOffer(moves=(), lines=(), sources=())
    names = {str(spend.category): spend.name for spend in spends}
    names[str(READY_TO_ASSIGN_SOURCE)] = "Ready to Assign"
    lines = tuple(
        CoverageLine(
            amount=move.amount,
            destination=names.get(str(move.destination), str(move.destination)),
            source=names.get(str(move.source), str(move.source)),
        )
        for move in plan.moves
    )
    uncovered = tuple(
        names.get(str(need.category), str(need.category))
        for need in plan.uncovered
    )
    return CoordinatedOffer(
        moves=plan.moves,
        lines=lines,
        sources=_to_source_views(sources, names),
        uncovered=uncovered,
    )


@activity.defn
async def send_coordinated_offer(subject: str, body: str, period: str) -> str:
    """Open the per-period coverage thread and return its id (SPEC §8, #46).

    A fresh email thread for the whole pass's coverage, keyed on the period
    (``alert_on_thread``), so a retry re-sends nothing. The workflow stamps
    ``BalanceThreadId`` with the returned id, so the owner's reply routes back
    to the coordinated balancer — reusing the existing balance dispatch route.
    """
    import asyncio

    from ynab_agent.budget.message import coverage_thread_label
    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings

    settings = Settings()
    mail = MailClient.from_env()
    label = coverage_thread_label(period)
    return await asyncio.to_thread(
        mail.alert_on_thread,
        inbox_id=settings.inbox,
        to=list(settings.owners),
        subject=subject,
        body=body,
        thread_label=label,
        update_label=label,
    )


@activity.defn
async def interpret_coordinated_reply(
    reply_text: str, plan_summary: str
) -> CoordinatedReplyResult:
    """Read the owner's reply to the one coordinated plan (SPEC §8, #46).

    Whole-plan only: a clear yes applies the offered plan as-is; a clear no
    declines; any request to change or partly apply it (or anything unclear)
    comes back as ``clarify`` with a question, so we never guess a write.
    """
    from ynab_agent.agentic.balance import (
        BalanceVerdict,
        CoordinatedReplyRequest,
    )
    from ynab_agent.agentic.balance import (
        interpret_coordinated_reply as run_reply,
    )

    reading = await run_reply(
        CoordinatedReplyRequest(
            reply_text=reply_text, plan_summary=plan_summary
        )
    )
    if reading.verdict is BalanceVerdict.APPLY:
        return CoordinatedReplyResult(verdict="apply")
    if reading.verdict is BalanceVerdict.DECLINE:
        return CoordinatedReplyResult(verdict="decline")
    return CoordinatedReplyResult(
        verdict="clarify",
        question=reading.question
        or 'Reply "do it" to apply the whole plan, or "no thanks".',
    )


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
    """Reply on the coordinated coverage thread, addressed to the owners (§8).

    The clarification and the apply/decline confirmation all reply on the
    per-period coverage thread (``thread_id``), so the owner sees one
    conversation for the pass. ``to`` is set explicitly because that thread's
    latest message is often the agent's own; without it AgentMail addresses the
    reply back to the agent and the owner never receives it. Idempotent on
    ``seq_label``.
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
