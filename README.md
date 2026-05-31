# YNAB Agent

A durable AI agent that triages, categorizes, splits, memos, and approves YNAB
transactions, driven by per-transaction email threads. It runs as a
deterministic Temporal spine (when to act, did-it-happen, the hard safety floor)
wrapping an agentic Pydantic AI middle (what the category/split/memo is). See
[`SPEC.md`](./SPEC.md) for the full design.

## Status

Early. Built inside-out, domain-core first: a pure, immutable domain model with
illegal states made unrepresentable, then the transaction-lifecycle state
machine, then Temporal and the MCP I/O layers.

## Layout

```
src/ynab_agent/   the package (domain core first; infrastructure later)
tests/            unit + property-based tests (hypothesis)
agents/           Many Hands Engineering loops (agentic linters; added per step)
SPEC.md           the design spec
```

## Toolchain

Python 3.13, managed with [uv](https://docs.astral.sh/uv/). Strict `mypy` (with
the Pydantic plugin) is the type safety net; `ruff` enforces the Google Python
style guide (including Google-style docstrings); `pytest` + `hypothesis` cover
behavior and invariants.

```bash
make sync     # create/refresh the dev environment
make fmt      # format + autofix
make check    # the full gate: format, lint, types, tests — keep it green
```

## Design principles

- **Ramp from cautious.** Autonomy is earned per-payee; no global trust switch.
- **A thin deterministic floor, then trust the model.** Caps, a circuit breaker,
  and idempotency bound catastrophe; above that, the model interprets.
- **Decisions are never final.** A late reply can reopen and rewrite an approved
  transaction; a correction is the highest-signal event in the system.
- **Make illegal states unrepresentable.** The domain types carry the rules.
