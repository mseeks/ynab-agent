"""The pure domain core: entities and the transaction-lifecycle state machine.

Everything here is immutable, free of I/O, and typed so that illegal states are
unrepresentable. The Temporal spine and the MCP adapters (added later) sit on
top of this; nothing here depends on them.
"""

from __future__ import annotations

from ynab_agent.domain.allocations import (
    FixedShare,
    PercentShare,
    ProposedAllocation,
    ProposedCategory,
    ProposedSplit,
    ResolvedAllocation,
    ResolvedCategory,
    ResolvedSplit,
    ResolvedSplitLine,
    SplitLine,
)
from ynab_agent.domain.config import DEFAULT_POLICY, LifecyclePolicy
from ynab_agent.domain.effects import Effect
from ynab_agent.domain.enums import (
    AutonomyLevel,
    AwaitingFlag,
    ClearedState,
    Confidence,
    DecidedBy,
    ReceiptStatus,
    RevisingOrigin,
    RuleSource,
    ShadowMode,
    SourceKind,
    TrustState,
    TxnState,
)
from ynab_agent.domain.events import LifecycleEvent, VerifyOutcome
from ynab_agent.domain.money import Money
from ynab_agent.domain.proposal import Decision, Proposal, ProposalSource
from ynab_agent.domain.receipt import Receipt, ReceiptLineItem
from ynab_agent.domain.rule import AmountRange, Rule, RuleAction, RuleMatch
from ynab_agent.domain.signals import InboundSignal, ReceiptSignal, ReplySignal
from ynab_agent.domain.state_machine import (
    Transition,
    TransitionKind,
    advance,
)
from ynab_agent.domain.transaction import (
    Applied,
    Archived,
    AutoApplied,
    AwaitingHuman,
    Discovered,
    Enriching,
    HoldAmazon,
    Lapsed,
    Open,
    Revising,
    Transaction,
    TxnCore,
    YnabSnapshot,
    born,
)

__all__ = [
    "DEFAULT_POLICY",
    "AmountRange",
    "Applied",
    "Archived",
    "AutoApplied",
    "AutonomyLevel",
    "AwaitingFlag",
    "AwaitingHuman",
    "ClearedState",
    "Confidence",
    "DecidedBy",
    "Decision",
    "Discovered",
    "Effect",
    "Enriching",
    "FixedShare",
    "HoldAmazon",
    "InboundSignal",
    "Lapsed",
    "LifecycleEvent",
    "LifecyclePolicy",
    "Money",
    "Open",
    "PercentShare",
    "Proposal",
    "ProposalSource",
    "ProposedAllocation",
    "ProposedCategory",
    "ProposedSplit",
    "Receipt",
    "ReceiptLineItem",
    "ReceiptSignal",
    "ReceiptStatus",
    "ReplySignal",
    "ResolvedAllocation",
    "ResolvedCategory",
    "ResolvedSplit",
    "ResolvedSplitLine",
    "Revising",
    "RevisingOrigin",
    "Rule",
    "RuleAction",
    "RuleMatch",
    "RuleSource",
    "ShadowMode",
    "SourceKind",
    "SplitLine",
    "Transaction",
    "Transition",
    "TransitionKind",
    "TrustState",
    "TxnCore",
    "TxnState",
    "VerifyOutcome",
    "YnabSnapshot",
    "advance",
    "born",
]
