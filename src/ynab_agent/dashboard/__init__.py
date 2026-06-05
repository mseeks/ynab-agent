"""The in-worker operations dashboard (SPEC §15).

A read-only, derive-on-request HTML surface the worker serves alongside itself
(same asyncio loop, same pod), reached privately over ``kubectl port-forward``.
Each request fans the source readers out concurrently (Temporal, ClickHouse,
YNAB, AgentMail, GitHub), assembles a pure read-model, and renders one
self-contained page; it stores nothing. Every reader degrades to an error string
→ a red dot, so a source being down yields a page with a warning, never a crash.

Modelled on froot's dashboard; see :mod:`ynab_agent.dashboard.server`.
"""
