"""Out-of-band operator alerting (a push to the owner's phone via ntfy).

This is *not* the agent's email channel — that is the agent's product surface
(per-transaction threads, SPEC §5), and mixing failure alerts into it would both
bury the alert and pollute the threads. Operational "something broke" pings go
here instead, to a private ntfy topic the owner subscribes to (SPEC §13: alert
via push, not email).
"""
