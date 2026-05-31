"""Learning events: the human decisions rule learning consumes (SPEC §9).

These are the *semantic* events — richer than the raw
:class:`~ynab_agent.domain.effects.RuleLearningKind` W2 emits. Each already
carries the :class:`~ynab_agent.domain.rule.RuleMatch` (the payee conditions to
key on) and the :class:`~ynab_agent.domain.rule.RuleAction` (the template to
encode); building those from a transaction's snapshot and decision is the
handler's job, so the pure transition core reasons only about trust.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.ids import RuleId
from ynab_agent.domain.rule import RuleAction, RuleMatch


class ConfirmCategory(Frozen):
    """A human accepted the proposed allocation (SPEC §9).

    Strengthens the rule for this ``(match, action)`` and advances its trust by
    one confirmation. ``rule_id`` names the rule that drove the proposal, if one
    did; ``None`` means a pure-human decision with no rule yet.
    """

    kind: Literal["confirm"] = "confirm"
    match: RuleMatch
    action: RuleAction
    rule_id: RuleId | None = None


class CorrectDecision(Frozen):
    """A human overturned a decision (SPEC §9). Corrections always win.

    Rewrites the *driving* rule's action to ``action`` and demotes it — the new
    preference is unproven. ``prior_rule_id`` is the rule that produced the
    overturned decision (so the *right* rule is demoted); ``None`` means the
    overturned decision had no rule, and the correction seeds a fresh one.
    """

    kind: Literal["correct"] = "correct"
    match: RuleMatch
    action: RuleAction
    prior_rule_id: RuleId | None = None


class ExplicitCommand(Frozen):
    """An explicit standing command — "always categorize X as Y" (SPEC §4.2).

    Blesses the rule for this ``(match, action)`` straight to ``trusted`` with
    ``source=human_explicit``; no confirmation count required.
    """

    kind: Literal["explicit"] = "explicit"
    match: RuleMatch
    action: RuleAction


LearningEvent = Annotated[
    ConfirmCategory | CorrectDecision | ExplicitCommand,
    Field(discriminator="kind"),
]
