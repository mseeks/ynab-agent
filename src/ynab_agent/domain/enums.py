"""The closed vocabularies of the domain, as string enums.

Every enum here names a *finite, total* set of states the SPEC defines. Using
enums (not bare strings) means an invalid value cannot be constructed, and the
state machine can branch on them with :func:`typing.assert_never` exhaustiveness
so mypy proves no case is missed.
"""

from __future__ import annotations

from enum import StrEnum


class TxnState(StrEnum):
    """The transaction lifecycle states (SPEC §3).

    ``OPEN`` is a *resting* state, not terminal: a late inbound there reopens
    the transaction. Silence ends in ``LAPSED`` (hand-off), never a guess.
    """

    DISCOVERED = "discovered"
    HOLD_AMAZON = "hold_amazon"
    ENRICHING = "enriching"
    AUTO_APPLIED = "auto_applied"
    AWAITING_HUMAN = "awaiting_human"
    APPLIED = "applied"
    OPEN = "open"
    LAPSED = "lapsed"
    REVISING = "revising"
    ARCHIVED = "archived"


class TrustState(StrEnum):
    """A rule's earned trust (SPEC §4.2); climbs by confirmation only."""

    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    TRUSTED = "trusted"


class AutonomyLevel(StrEnum):
    """Per-payee autonomy (SPEC §4.2).

    ``L1`` does not exist in the SPEC; the ladder is L0 → L2 → L3.
    """

    L0_CONFIRM = "l0_confirm"
    L2_TRUSTED_AUTO = "l2_trusted_auto"
    L3_SILENT = "l3_silent"


class Confidence(StrEnum):
    """How a proposal is worded — framing only, never a gate (SPEC §4.1)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DecidedBy(StrEnum):
    """Who decided — a human reply, or the agent under a blessed rule."""

    HUMAN = "human"
    AGENT = "agent"


class SourceKind(StrEnum):
    """A proposal's signal sources, strongest first (SPEC §4.1)."""

    RULE = "rule"
    YNAB_HISTORY = "ynab_history"
    RECEIPT = "receipt"
    MODEL = "model"
    WEB_SEARCH = "web_search"


class RuleSource(StrEnum):
    """How a rule came to exist (SPEC §1, §9)."""

    HUMAN_EXPLICIT = "human_explicit"
    LEARNED = "learned"


class ReceiptStatus(StrEnum):
    """A parked receipt's lifecycle in the join store (SPEC §6)."""

    PARKED = "parked"
    MATCHED = "matched"
    ASKED = "asked"
    EXPIRED = "expired"


class ClearedState(StrEnum):
    """YNAB cleared status; ``RECONCILED`` is the reconciliation guard (§3)."""

    UNCLEARED = "uncleared"
    CLEARED = "cleared"
    RECONCILED = "reconciled"


class FlagColor(StrEnum):
    """YNAB flag colors (one person-tag channel option; SPEC §4.3)."""

    RED = "red"
    ORANGE = "orange"
    YELLOW = "yellow"
    GREEN = "green"
    BLUE = "blue"
    PURPLE = "purple"


class ReviewVerdict(StrEnum):
    """The agent-powered safety review's one-way ratchet (SPEC §0.6).

    It can only hold back (``ESCALATE_TO_HUMAN``) or stay within the blessed
    grant (``PROCEED``); it never expands autonomy.
    """

    PROCEED = "proceed"
    ESCALATE_TO_HUMAN = "escalate_to_human"


class OfferVerdict(StrEnum):
    """How the owner answered a proactive autonomy offer (SPEC §14.7 3b).

    The model reads a free-form reply into one of these: ``ACCEPT`` blesses the
    rule, ``DECLINE`` keeps proposing, and ``UNCLEAR`` (the safe default on any
    doubt) neither blesses nor closes — the offer keeps waiting for a clear
    answer, since granting autonomy must never happen on ambiguity.
    """

    ACCEPT = "accept"
    DECLINE = "decline"
    UNCLEAR = "unclear"


class RevisingOrigin(StrEnum):
    """Where a REVISING run was entered from (SPEC §3, no-change exit rule).

    Governs the no-change exit: a revision of an already-applied txn returns to
    ``OPEN``; one entered from ``LAPSED`` (never applied) re-arms patience in
    ``AWAITING_HUMAN`` instead, so an unhandled txn is never mislabeled resting.
    """

    APPLIED = "applied"
    LAPSED = "lapsed"


class AwaitingFlag(StrEnum):
    """Why a txn awaits a human (SPEC §3 REVISING verify outcomes)."""

    NONE = "none"
    POSSIBLY_INCONSISTENT = "possibly_inconsistent"
    DIVERGED = "diverged"
