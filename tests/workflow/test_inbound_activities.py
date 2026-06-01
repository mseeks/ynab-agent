"""Tests for the pure helpers behind the W2 inbound activities (SPEC §3, §5).

``interpret_inbound`` and ``converge`` are thin glue over the interpret/converge
agents (``tests/agentic``) and the verify policy (``tests/policy``), driven
end-to-end by the workflow's mocks. The logic unique to the activity layer is
extracting the single proposed category and summarising an end-state for a
divergence note — covered here.
"""

from __future__ import annotations

from ynab_agent.domain.allocations import (
    PercentShare,
    ProposedCategory,
    ProposedSplit,
    ResolvedCategory,
    SplitLine,
)
from ynab_agent.domain.enums import Confidence
from ynab_agent.domain.ids import CategoryId
from ynab_agent.domain.proposal import Proposal
from ynab_agent.policy.converge import TargetState
from ynab_agent.workflow.activities import (
    _proposed_category_id,
    _target_summary,
)

_NAMES = {"dining": "Dining Out", "coffee": "Coffee"}


def _proposal(allocation: object) -> Proposal:
    return Proposal(
        allocation=allocation,  # type: ignore[arg-type]
        confidence=Confidence.MEDIUM,
        rationale="because",
    )


def test_proposed_category_id_for_single_category() -> None:
    proposal = _proposal(ProposedCategory(category=CategoryId("dining")))
    assert _proposed_category_id(proposal) == "dining"


def test_proposed_category_id_none_for_split() -> None:
    split = ProposedSplit(
        lines=(
            SplitLine(
                share=PercentShare(percent=50), category=CategoryId("dining")
            ),
            SplitLine(
                share=PercentShare(percent=50), category=CategoryId("coffee")
            ),
        )
    )
    assert _proposed_category_id(_proposal(split)) is None


def test_proposed_category_id_none_for_no_proposal() -> None:
    assert _proposed_category_id(None) is None


def test_target_summary_names_category_and_memo() -> None:
    target = TargetState(
        allocation=ResolvedCategory(category=CategoryId("dining")),
        memo="team lunch",
    )
    assert _target_summary(target, _NAMES) == "Dining Out — team lunch"


def test_target_summary_without_memo() -> None:
    target = TargetState(
        allocation=ResolvedCategory(category=CategoryId("coffee"))
    )
    assert _target_summary(target, _NAMES) == "Coffee"


def test_target_summary_handles_unreadable_state() -> None:
    assert _target_summary(None, _NAMES) == "(could not read)"


def test_target_summary_falls_back_to_id() -> None:
    target = TargetState(
        allocation=ResolvedCategory(category=CategoryId("mystery"))
    )
    assert _target_summary(target, _NAMES) == "mystery"
