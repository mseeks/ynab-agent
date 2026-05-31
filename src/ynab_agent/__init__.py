"""YNAB Agent: durable, email-driven YNAB transaction triage.

An AI agent that triages, categorizes, splits, memos, and approves YNAB
transactions, driven by per-transaction email threads. Built as a deterministic
Temporal spine wrapping an agentic Pydantic AI middle (see ``SPEC.md``).

This package is organized inside-out: a pure, immutable domain core (entities
and the transaction-lifecycle state machine, with illegal states made
unrepresentable) sits beneath later infrastructure layers (Temporal, MCP I/O).
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
