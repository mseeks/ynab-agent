"""Many Hands Engineering loops for the YNAB agent.

Each loop is a small, reversible cycle — *signal → action → verification →
report* — run as part of the development cycle (see ``many-hands-engineering``).
A loop is a deterministic signal (computed by harness code) plus a locked-down,
read-only Claude Agent SDK pass that verifies each hit in context and emits a
strict, cite-or-omit map. Loops propose; humans address.

The shared harness lives in :mod:`agents.lib`; each loop is its own module.
"""
