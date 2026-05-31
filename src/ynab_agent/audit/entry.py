"""The append-only audit log — the "why did it do that" record (SPEC §9, §3).

Every consequential moment in a transaction's life is recorded as one
:class:`AuditEntry`: a state change, a gate ruling, a committed decision, an
outbound message, a learning update. Replaying the log (plus the rule table)
answers *why* the agent did what it did.

The log is **append-only by construction**: :meth:`AuditLog.append` is the only
way to add an entry, and it stamps a monotonically increasing ``seq`` — there is
no API to mutate or reorder. The workflow carries only a pointer to the
externalized log (``TxnCore.audit_log_ref``); these types are how an entry is
shaped before it is written and after it is read back.

The event payloads lean on the domain vocabulary (``TxnState`` etc.) but record
cross-layer verdicts (the gate, rule learning) as plain strings, so a historical
entry never breaks when those policy enums are refactored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from ynab_agent.domain.base import Frozen
from ynab_agent.domain.enums import DecidedBy, TrustState, TxnState
from ynab_agent.domain.ids import RuleId


class StateChanged(Frozen):
    """The transaction moved to a new lifecycle state."""

    kind: Literal["state_changed"] = "state_changed"
    to_state: TxnState
    trigger: str
    from_state: TxnState | None = None


class Gated(Frozen):
    """The autonomy gate ruled on whether a proposal may auto-apply."""

    kind: Literal["gated"] = "gated"
    verdict: str
    reason: str = ""
    rule_id: str | None = None


class Decided(Frozen):
    """A decision was committed (the what, the who, and the gating rule)."""

    kind: Literal["decided"] = "decided"
    decided_by: DecidedBy
    approved: bool
    summary: str
    rule_id: str | None = None


class MessageSent(Frozen):
    """An outbound thread message, recorded before sending (the dedup key)."""

    kind: Literal["message_sent"] = "message_sent"
    action_seq: int
    purpose: str


class Learned(Frozen):
    """Rule learning updated a rule's trust on a confirm/correct/command."""

    kind: Literal["learned"] = "learned"
    change: str
    rule_id: RuleId
    trust: TrustState


AuditEvent = Annotated[
    StateChanged | Gated | Decided | MessageSent | Learned,
    Field(discriminator="kind"),
]


class AuditEntry(Frozen):
    """One recorded event with its sequence number and timestamp."""

    seq: int = Field(ge=0)
    at: datetime
    event: AuditEvent


class AuditLog(Frozen):
    """An append-only sequence of audit entries for one transaction."""

    entries: tuple[AuditEntry, ...] = ()

    @property
    def next_seq(self) -> int:
        """The sequence number the next appended entry will receive."""
        return len(self.entries)

    def append(self, event: AuditEvent, *, at: datetime) -> AuditLog:
        """Return a new log with ``event`` appended at the next ``seq``.

        The only way to grow a log; ``seq`` is assigned here, so it is always
        contiguous and monotonic and an entry can never be reordered or edited.
        """
        entry = AuditEntry(seq=self.next_seq, at=at, event=event)
        return AuditLog(entries=(*self.entries, entry))
