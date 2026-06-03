# Proposed loops

Candidate Many Hands Engineering loops — *not yet implemented*. Each keeps the
framework shape (a deterministic Python **signal** + a locked-down read-only
agent pass → a three-bucket *cite-or-omit* map; loops propose, you address) and
guards a structural invariant that the learning/autonomy work (SPEC §14) and
general feature growth put at risk. They are the anti-spaghetti backstop: as the
W5/autonomy surface grows, these keep the layering, the safety invariants, and
the spec honest *mechanically* rather than by reviewer memory.

Listed roughly by leverage for the current direction.

| Loop | Signal (deterministic) | Output (three-bucket map) | Why now |
|---|---|---|---|
| `pure-core-isolation` | AST import scan of the pure layers (`domain/`, `learn/`, `policy/`, `ingest/`, `budget/`) for any import of an I/O / framework stack (`workflow`, `temporalio`, `mail`, `ynab`, `agentic`, `pydantic_ai`, `httpx`) | *Leak* (push the import to the spine/activity) / *Clean* (pure→pure only) / *Judgment-heavy* | The DDD seam (pure core ↔ Temporal spine) is what keeps this testable; W5 adds three pure modules + a workflow, and the easy mistake is reaching from `learn/` into a client. `sandbox-imports` only guards `@workflow.defn`/`@activity.defn` files — this guards the *core*. |
| `autonomy-earned` | regex/AST sweep for autonomy grants outside the sanctioned seam: an assignment to `TrustState.TRUSTED`, a `GateVerdict.AUTO`, or `source = …HUMAN_EXPLICIT` anywhere but `learn/transitions.py`, `learn/registry.py`, `policy/gate.py` (+ tests) | *Ungoverned grant* (route through the transition/bless core) / *Sanctioned* (the seam itself) / *Judgment-heavy* | SPEC §14's load-bearing invariant — "earned or blessed, never on silence." A stray `trust=TRUSTED` in a new feature silently hands out autonomy. Make the seam the *only* place it can happen. |
| `variant-exhaustiveness` | collect each discriminated-union's members (`Effect`, `InboundSignal`, `LearningEvent`, `DispatchDecision`, the registry signals) and flag any member never named in a `case`/dispatch over that union | *Unwired variant* (add the `case`) / *Wired* / *Judgment-heavy* (handled generically) | The classic feature-growth bug: add an `Effect`/signal/state, forget to handle it, it silently no-ops. As the autonomy effects multiply (flag, FYI, bless), exhaustiveness is the guard. Complements mypy's `assert_never` by catching the *missing branch at the call site*. |
| `durable-state-bound` | AST scan of `@workflow.defn` files for a long-lived shape — a `wait_condition` / signal loop — that lacks a `continue_as_new` *and* an `is_continue_as_new_suggested()` (or equivalent history-bound) | *Unbounded* (add continue-as-new) / *Bounded* (returns / short-lived) / *Judgment-heavy* | The registry and poll are perpetual singletons; a new durable workflow that accumulates signals without continuing-as-new grows history forever and eventually wedges replay. A Temporal-specific guard with no Revisionist analogue (like `determinism`). |
| `spec-citation` | parse `SPEC §N(.M)` references in docstrings/comments and assert each cited section exists in `SPEC.md`; assert every `@workflow.defn` class maps to a W-number with a SPEC section | *Dangling cite* (fix ref) / *Resolves* / *Judgment-heavy* (semantic drift for the agent) | This is a spec-driven codebase; §14 just renumbered the autonomy story. Keep the dense `SPEC §x` cross-refs from rotting as sections move — a focused extension of `doc-coherence`. |
| `autonomy-visibility` | locate every site producing an agent decision (`DecidedBy.AGENT` / `build_auto_decision`) and check the same path also emits the visibility triad (flag set, FYI message, memo write, audit entry) | *Silent auto-action* (wire the FYI/flag/memo) / *Visible* / *Judgment-heavy* | SPEC §14.5: the owner wants *full visibility* for every auto-action. The harder one to express deterministically (the signal flags candidates; the agent verifies the triad in context), so it's a Stage-2 "purpose-built scan" loop, not a regex sweep. |

## Notes

- **Shape discipline.** Each is one signal + one locked-down prompt over
  `lib.run_loop`; none broadens an existing loop. `pure-core-isolation`,
  `autonomy-earned`, `spec-citation` are regex/AST sweeps; `variant-exhaustiveness`,
  `durable-state-bound`, `autonomy-visibility` bring a purpose-built scan (the
  Stage-2 pattern, per the loops README).
- **Sequencing.** `pure-core-isolation` and `autonomy-earned` are worth landing
  *alongside* the gate-in-W2 increment (§14.7 step 2) — they guard exactly the
  code that increment touches. `autonomy-visibility` pairs with the visibility
  increment (step 5). `variant-exhaustiveness` and `durable-state-bound` are
  general growth guards, land anytime. `spec-citation` is cheap insurance now
  that §14 reshuffled references.
- **Cost.** Same as existing loops: read-only, `max_budget_usd` cap, no findings
  cache (addressed = fixed). Wire as `agents/<name>.py` + a `loop-<name>` make
  target + a `test_<name>_sweep.py` (the deterministic signal, unit-tested like
  the others).
