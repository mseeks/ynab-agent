"""The budget-balancer agent: propose and read coverage moves (SPEC §8).

The agentic half of W7. Two model calls, both high-context and both given a
**calculator tool** — models are unreliable at arithmetic, so they call
``add``/``subtract``/``multiply``/``sum_amounts`` while reasoning instead of
doing mental math:

- :func:`propose_balance` sees the whole budget picture (the needy category, the
  shortfall, every source category's available funds, and Ready-to-Assign) and
  returns several distinct :class:`~ynab_agent.budget.balance.BalanceOption` s,
  each with a plain-English rationale.
- :func:`interpret_balance_reply` reads the owner's free-text answer ("do option
  2 but only $50", "take it from dining instead", "no thanks") into a concrete
  plan, a decline, or a clarifying question.

The calculator is a *reasoning aid only*: the binding amounts are recomputed in
exact :class:`~ynab_agent.domain.money.Money` by the ``to_*`` seams, and the
deterministic guard (``budget.balance.validate_option`` /
``policy.floor.check_budget_move_floor``) has the final say before a write. The
model never authorizes a move on its own (SPEC §0.5 principle 6).

The model is injected per run so tests drive a ``TestModel`` offline; production
uses :func:`~ynab_agent.agentic.model.build_model` (Ollama/Gemma).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from pydantic import Field
from pydantic_ai import Agent

from ynab_agent.agentic.model import run_structured
from ynab_agent.budget.balance import (
    ApplyMoves,
    BalanceOption,
    BalanceOutcome,
    BudgetMove,
    ClarifyBalance,
    DeclineBalance,
)
from ynab_agent.domain.base import Frozen
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.money import Money

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic_ai.models import Model


# --- The calculator tool the agents call so their arithmetic is exact. --------
# Plain functions over dollar amounts; pydantic AI registers each as a tool
# (name from the function, description from the docstring). They are pure and do
# no I/O, so they run in-process inside the activity, never a workflow sandbox.


def add(a: float, b: float) -> float:
    """Add two dollar amounts and return the sum."""
    return a + b


def subtract(a: float, b: float) -> float:
    """Subtract b from a (a - b): a shortfall, or funds left after a pull."""
    return a - b


def multiply(a: float, b: float) -> float:
    """Multiply a by b: a fraction of an amount (0.5 for half, 0.3 for 30%)."""
    return a * b


def sum_amounts(amounts: list[float]) -> float:
    """Add up a list of dollar amounts (e.g. the total several sources give)."""
    return float(sum(amounts))


_CALCULATOR_TOOLS: list[Callable[..., float]] = [
    add,
    subtract,
    multiply,
    sum_amounts,
]


# --- Shared move shape: pull ``amount`` from a source, dest implied. ----------


class MoveSpec(Frozen):
    """Pull ``amount`` dollars from ``source_category_id``.

    The destination is always the needy category, injected by the ``to_*`` seam,
    so the model can't fund the wrong category and has one fewer thing to get
    right. ``amount`` is a positive dollar figure.
    """

    source_category_id: str
    amount: float


# --- Proposing options (the high-context balancing call). ---------------------


class SourceFunds(Frozen):
    """A category the balancer may pull from, with its available funds."""

    id: str
    name: str
    available: float
    kind: str


class BalanceContext(Frozen):
    """The whole budget picture the model balances over (SPEC §8)."""

    needy_category_id: str
    needy_category_name: str
    shortfall: float
    overspend_note: str
    sources: tuple[SourceFunds, ...] = Field(min_length=1)


class ProposedOption(Frozen):
    """One model-proposed way to cover the shortfall, with its rationale."""

    label: str
    moves: tuple[MoveSpec, ...]
    rationale: str


class BalanceProposal(Frozen):
    """The model's several coverage options (mapped to domain options)."""

    options: tuple[ProposedOption, ...]


_PROPOSE_SYSTEM_PROMPT = """\
You help balance a household budget. One category is over (or heading over) its
budget this month and needs to be covered by moving already-budgeted money from
other categories. No money leaves any account — this is a reallocation, fully
reversible.

You are given the needy category, the shortfall to cover (in dollars), a note on
how it is tracking, and a list of source categories you may pull from, each with
an id, a name, how much is available, and a kind ("ready-to-assign" or
"category"). Prefer Ready-to-Assign and clearly over-funded categories; avoid
draining a category to zero when another source has room.

Propose 2 to 4 DISTINCT options, each a genuinely different way to cover the
shortfall (different sources or a different split). For each option give:
  - a short `label` (e.g. "From Ready to Assign", "Split across buffers"),
  - the `moves`: which `source_category_id` to pull from and how much, and
  - a one-sentence `rationale` explaining the tradeoff in plain English.

Hard rules: every option's moves must add up to AT LEAST the shortfall; never
pull more from a source than it has available; only use the listed source ids.

You are bad at mental arithmetic, so use the calculator tools (`sum_amounts`,
`add`, `subtract`, `multiply`) for every sum, difference, and fraction — do not
eyeball the numbers."""

_PROPOSE_AGENT: Agent[None, BalanceProposal] = Agent(
    output_type=BalanceProposal,
    system_prompt=_PROPOSE_SYSTEM_PROMPT,
    tools=_CALCULATOR_TOOLS,
)


def _format_context(context: BalanceContext) -> str:
    """Render the budget picture as the agent's user prompt."""
    lines = [
        f"Needy category: {context.needy_category_name} "
        f"(id: {context.needy_category_id})",
        f"Shortfall to cover: ${context.shortfall:.2f}",
        f"Situation: {context.overspend_note}",
        "Sources you may pull from:",
    ]
    lines.extend(
        f"  - {source.name} (id: {source.id}, kind: {source.kind}): "
        f"${source.available:.2f} available"
        for source in context.sources
    )
    return "\n".join(lines)


async def propose_balance(
    context: BalanceContext, *, model: Model | None = None
) -> BalanceProposal:
    """Run the option-proposing agent for one overspend (SPEC §8)."""
    return await run_structured(
        _PROPOSE_AGENT,
        _format_context(context),
        output_type=BalanceProposal,
        model=model,
    )


def to_options(
    proposal: BalanceProposal, *, destination: CategoryId
) -> tuple[BalanceOption, ...]:
    """Map the model's proposal onto domain options (SPEC §8).

    Every move funds ``destination`` (the needy category), and dollar amounts
    become exact :class:`Money` here — the model's numbers never reach YNAB raw.
    """
    return tuple(
        BalanceOption(
            label=option.label,
            moves=tuple(_to_move(spec, destination) for spec in option.moves),
            rationale=option.rationale,
        )
        for option in proposal.options
    )


# --- Reading the owner's free-text reply. -------------------------------------


class BalanceVerdict(StrEnum):
    """What an owner's reply to a balance offer asked for."""

    APPLY = "apply"
    DECLINE = "decline"
    UNCLEAR = "unclear"


class OfferedOption(Frozen):
    """An option as it was presented, so the model can resolve "option 2"."""

    label: str
    moves: tuple[MoveSpec, ...]
    rationale: str


class BalanceReplyRequest(Frozen):
    """The owner's reply plus the context needed to read it."""

    reply_text: str
    needy_category_name: str
    shortfall: float
    options: tuple[OfferedOption, ...]
    sources: tuple[SourceFunds, ...]


class BalanceReading(Frozen):
    """The agent's read of the reply (mapped to a domain BalanceOutcome)."""

    verdict: BalanceVerdict
    moves: tuple[MoveSpec, ...] = ()
    question: str | None = None


_REPLY_SYSTEM_PROMPT = """\
You read one reply an account owner sent in answer to a budget-coverage offer.
The agent had emailed several numbered options for covering an over-budget
category by moving money from other categories, and asked which to use.

You are given their reply, the needy category and shortfall, the options that
were offered (each with a label, its moves, and a rationale), and the source
categories with their available funds.

Decide what the reply means and set `verdict`:
  - `apply` — they approved a way to cover it. Output the exact `moves` to make
    (each: a `source_category_id` from the list and a dollar `amount`). If they
    picked an option as-is, copy that option's moves. If they modified it ("only
    $50", "take it from dining instead", "half from each"), output the modified
    moves. The moves must add up to at least the shortfall and never exceed any
    source's available funds.
  - `decline` — a clear no ("no thanks", "leave it", "I'll handle it myself").
  - `unclear` — a question, a different topic, or anything you are unsure about;
    set a short `question` to send back.

Moving money is consequential, so when in doubt choose `unclear`, never `apply`.
You are bad at mental arithmetic, so use the calculator tools (`sum_amounts`,
`add`, `subtract`, `multiply`) for every amount you compute — never eyeball."""

_REPLY_AGENT: Agent[None, BalanceReading] = Agent(
    output_type=BalanceReading,
    system_prompt=_REPLY_SYSTEM_PROMPT,
    tools=_CALCULATOR_TOOLS,
)


def _format_reply(request: BalanceReplyRequest) -> str:
    """Render the reply and its context as the agent's user prompt."""
    lines = [
        f"Their reply: {request.reply_text}",
        f"Needy category: {request.needy_category_name}",
        f"Shortfall to cover: ${request.shortfall:.2f}",
        "Options that were offered:",
    ]
    for index, option in enumerate(request.options, start=1):
        moves = ", ".join(
            f"${spec.amount:.2f} from {spec.source_category_id}"
            for spec in option.moves
        )
        lines.append(f"  {index}. {option.label}: {moves} — {option.rationale}")
    lines.append("Sources and available funds:")
    lines.extend(
        f"  - {source.name} (id: {source.id}): ${source.available:.2f}"
        for source in request.sources
    )
    return "\n".join(lines)


async def interpret_balance_reply(
    request: BalanceReplyRequest, *, model: Model | None = None
) -> BalanceReading:
    """Run the reply-reading agent for one message (SPEC §8)."""
    return await run_structured(
        _REPLY_AGENT,
        _format_reply(request),
        output_type=BalanceReading,
        model=model,
    )


def to_balance_outcome(
    reading: BalanceReading, *, destination: CategoryId
) -> BalanceOutcome:
    """Map the reply reading onto a domain outcome (SPEC §8).

    ``apply`` with no concrete moves falls back to a clarifying question — the
    safe move is always to ask, never to guess a write (cf. ``interpret``).
    """
    match reading.verdict:
        case BalanceVerdict.APPLY:
            moves = tuple(_to_move(spec, destination) for spec in reading.moves)
            if not moves:
                return ClarifyBalance(
                    question=(
                        "Which option should I apply, or how would you like to "
                        "cover it?"
                    )
                )
            return ApplyMoves(moves=moves)
        case BalanceVerdict.DECLINE:
            return DeclineBalance()
        case BalanceVerdict.UNCLEAR:
            return ClarifyBalance(
                question=reading.question
                or "Which option should I use, or how should I cover it?"
            )
    assert_never(reading.verdict)


def _money(dollars: float) -> Money:
    """A dollar figure from the model as exact Money (rounded to the cent).

    The model speaks floats; rounding to cents before the Decimal conversion
    keeps binary-float noise out of the milliunit amount that reaches YNAB.
    """
    return Money.from_currency(str(round(dollars, 2)))


def _to_move(spec: MoveSpec, destination: CategoryId) -> BudgetMove:
    """A model move spec as a domain move into the needy ``destination``."""
    return BudgetMove(
        source=CategoryId(spec.source_category_id),
        destination=destination,
        amount=_money(spec.amount),
    )
