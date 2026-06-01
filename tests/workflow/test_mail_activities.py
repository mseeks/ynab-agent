"""Tests for the deterministic helpers behind the mail activities (SPEC §5).

The activity bodies themselves are thin glue over ``MailClient`` (idempotency
covered in ``tests/mail``), the compose agent (``tests/agentic``), and the YNAB
client — exercised end-to-end via the workflow's mocks. What is unique here is
the pure logic: the idempotency labels, the templated subject, and turning a
proposed allocation into a human category display.
"""

from __future__ import annotations

import datetime

from ynab_agent.domain.allocations import (
    PercentShare,
    ProposedCategory,
    ProposedSplit,
    SplitLine,
)
from ynab_agent.domain.ids import AccountId, CategoryId, YnabTransactionId
from ynab_agent.domain.money import Money
from ynab_agent.domain.transaction import YnabSnapshot
from ynab_agent.workflow.activities import (
    _allocation_display,
    _seq_label,
    _subject,
    _txn_label,
)

_NAMES = {"dining": "Dining Out", "coffee": "Coffee", "gifts": "Gifts"}


def _snapshot() -> YnabSnapshot:
    return YnabSnapshot(
        ynab_id=YnabTransactionId("t-1"),
        account=AccountId("a1"),
        payee="Blue Bottle",
        amount=Money.from_currency("-4.50"),
        txn_date=datetime.date(2026, 5, 30),
    )


def test_txn_and_seq_labels_are_namespaced() -> None:
    assert _txn_label("t-1") == "yatxn-t-1"
    assert _seq_label("t-1", 3) == "yaseq-t-1-3"


def test_subject_names_the_payee_and_amount() -> None:
    subject = _subject(_snapshot())
    assert "Blue Bottle" in subject
    assert "-4.50" in subject


def test_allocation_display_single_category_uses_name() -> None:
    allocation = ProposedCategory(category=CategoryId("dining"))
    assert _allocation_display(allocation, _NAMES) == "Dining Out"


def test_allocation_display_split_joins_names() -> None:
    allocation = ProposedSplit(
        lines=(
            SplitLine(
                share=PercentShare(percent=50), category=CategoryId("dining")
            ),
            SplitLine(
                share=PercentShare(percent=50), category=CategoryId("coffee")
            ),
        )
    )
    assert _allocation_display(allocation, _NAMES) == "Dining Out + Coffee"


def test_allocation_display_falls_back_to_id_when_unknown() -> None:
    allocation = ProposedCategory(category=CategoryId("mystery"))
    assert _allocation_display(allocation, _NAMES) == "mystery"
