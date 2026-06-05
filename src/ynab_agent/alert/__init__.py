"""Failure-alert dedup: the pure policy that keeps alerting from bombarding.

The owner asked for *immediate, per-failure* alerts that nonetheless never
flood. Those two goals meet here: a small append-only ledger of "what was
alerted, when" plus two pure rules — a per-key cooldown (the same transaction
re-failing every poll tick alerts once, not hourly) and a global rate cap (a
systemic break that fails N transactions at once alerts a few times, not N).

Pure and frozen like the rest of the domain; the durable
:class:`~ynab_agent.workflow.alert_ledger_workflow.AlertLedgerWorkflow` wraps it
as Temporal state, mirroring how ``learn.registry`` sits under the registry
workflow.
"""
