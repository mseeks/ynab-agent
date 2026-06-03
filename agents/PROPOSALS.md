# Proposed loops

Candidate Many Hands Engineering loops — *not yet implemented*. Each keeps the
framework shape (a deterministic Python **signal** + a locked-down read-only
agent pass → a three-bucket *cite-or-omit* map; loops propose, you address) and
guards a structural invariant that the learning/autonomy work (SPEC §14) and
general feature growth put at risk. They are the anti-spaghetti backstop: as the
W5/autonomy surface grows, these keep the layering, the safety invariants, and
the spec honest *mechanically* rather than by reviewer memory.

> **Shipped from this list:** `pure-core-isolation`, `variant-exhaustiveness`,
> and `spec-citation` are now implemented (see `README.md`). The three below
> remain proposed.

| Loop | Signal (deterministic) | Output (three-bucket map) | Why now |
|---|---|---|---|
| `autonomy-earned` | regex/AST sweep for autonomy grants outside the sanctioned seam: an assignment to `TrustState.TRUSTED`, a `GateVerdict.AUTO`, or `source = …HUMAN_EXPLICIT` anywhere but `learn/transitions.py`, `learn/registry.py`, `policy/gate.py` (+ tests) | *Ungoverned grant* (route through the transition/bless core) / *Sanctioned* (the seam itself) / *Judgment-heavy* | SPEC §14's load-bearing invariant — "earned or blessed, never on silence." A stray `trust=TRUSTED` in a new feature silently hands out autonomy. Make the seam the *only* place it can happen. |
| `durable-state-bound` | AST scan of `@workflow.defn` files for a long-lived shape — a `wait_condition` / signal loop — that lacks a `continue_as_new` *and* an `is_continue_as_new_suggested()` (or equivalent history-bound) | *Unbounded* (add continue-as-new) / *Bounded* (returns / short-lived) / *Judgment-heavy* | The registry and poll are perpetual singletons; a new durable workflow that accumulates signals without continuing-as-new grows history forever and eventually wedges replay. A Temporal-specific guard with no Revisionist analogue (like `determinism`). |
| `autonomy-visibility` | locate every site producing an agent decision (`DecidedBy.AGENT` / `build_auto_decision`) and check the same path also emits the visibility triad (flag set, FYI message, memo write, audit entry) | *Silent auto-action* (wire the FYI/flag/memo) / *Visible* / *Judgment-heavy* | SPEC §14.5: the owner wants *full visibility* for every auto-action. The harder one to express deterministically (the signal flags candidates; the agent verifies the triad in context), so it's a Stage-2 "purpose-built scan" loop, not a regex sweep. |

## Notes

- **Shape discipline.** Each is one signal + one locked-down prompt over
  `lib.run_loop`; none broadens an existing loop. `autonomy-earned` is a
  regex/AST sweep; `durable-state-bound` and `autonomy-visibility` bring a
  purpose-built scan (the Stage-2 pattern, per the loops README).
- **Sequencing.** `autonomy-earned` is worth landing *alongside* the gate-in-W2
  increment (§14.7 step 2) — it guards exactly the code that increment touches.
  `autonomy-visibility` pairs with the visibility increment (step 5).
  `durable-state-bound` is a general growth guard, land anytime.
- **Cost.** Same as existing loops: read-only, `max_budget_usd` cap, no findings
  cache (addressed = fixed). Wire as `agents/<name>.py` + a `loop-<name>` make
  target + a `test_<name>_sweep.py` (the deterministic signal, unit-tested like
  the others).
