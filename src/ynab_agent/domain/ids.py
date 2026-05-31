"""Branded identifier types.

Each id is a distinct :func:`typing.NewType` over ``str``. They carry no extra
runtime cost, but mypy treats them as separate types, so a ``ThreadId`` can
never be passed where a ``YnabTransactionId`` is expected — a whole class of
mix-up bugs becomes unrepresentable. Construct one by wrapping a string, e.g.
``YnabTransactionId("abc-123")``.
"""

from __future__ import annotations

from typing import NewType

# YNAB-side identifiers.
YnabTransactionId = NewType("YnabTransactionId", str)
AccountId = NewType("AccountId", str)
CategoryId = NewType("CategoryId", str)
PayeeId = NewType("PayeeId", str)
ImportId = NewType("ImportId", str)

# Agent-side identifiers.
ThreadId = NewType("ThreadId", str)
ReceiptId = NewType("ReceiptId", str)
RuleId = NewType("RuleId", str)
MessageId = NewType("MessageId", str)

# A structured "who" tag for split lines (Matthew vs. wife); YNAB has no native
# field for this, so it is a field of record on the (sub)transaction.
PersonTag = NewType("PersonTag", str)
