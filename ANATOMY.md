# Anatomy — ynab-agent as one living map

> A spec for an interactive **anatomy diagram** of ynab-agent: the whole system as
> a single graph, walked statically from the code (every workflow, activity,
> agentic step, and external system as a node; every call, dispatch, and signal as
> an edge), then overlaid with **live traffic from traces** so you can see
> structure *and* flow at once. The static skeleton is exact and complete; the
> trace heat shows what actually runs and how hard. Crucially, the skeleton is
> **built from source on load**, so it can never go stale (§3, §7).
>
> Read [`SPEC.md`](./SPEC.md) first — this view *renders* the workflow map (§2),
> the transaction lifecycle (§3), and the dispatch/learning/balancing pipelines it
> describes; it doesn't redefine them.

## 1. Why this exists

ynab-agent is harder to hold in your head than most Temporal apps, on purpose: it
is a deterministic spine wrapped around an agentic middle (SPEC §0.5), and its
flows are **human-in-the-loop** — a transaction can pause for days waiting on an
email reply that arrives through a completely different workflow. Fourteen
workflows, ten LLM agents, durable ledger singletons, and signal-with-start hops
between them. The wiring is real and it is a lot.

This is the cure: one picture you can re-find every time. In order:

1. **Where does it live in the tree?** Trace any agentic step (say `match_receipt`)
   back to the workflow and pipeline that drive it.
2. **What's wired to what?** See the real edges — especially the indirect ones:
   an activity that *signals* a workflow waiting half a system away (the inbound
   reply path), an activity that *starts* a child offer workflow, a ledger that
   loops on itself.
3. **What's hot?** Overlay call counts and latency so the busy agents and the cold
   corners are obvious — which LLM step runs most, where the time goes.

It is **not** a per-trace debugger (HyperDX does waterfalls) and not a replacement
for the in-worker ops dashboard (SPEC §15). This is the *map*, not the gauges.

## 2. The model: two layers fused

### 2.1 Node kinds

| Kind | What it is | How it's found (static) | ynab-agent examples |
|---|---|---|---|
| **workflow** | a `@workflow.defn` class | decorator scan | `TransactionWorkflow`, `DispatchWorkflow`, `BudgetBalanceWorkflow` |
| ↳ *role facet* | driver / loop / offer / **ledger** (durable singleton) | shape of `run()` (continue_as_new + query/signal only ⇒ ledger) | `RuleRegistryWorkflow` (ledger), `PollWorkflow` (loop) |
| **activity** | an `@activity.defn` function | decorator scan | `fetch_snapshot`, `commit_to_ynab`, `send_thread_message` |
| ↳ *agentic flag* | activity that runs an LLM step | calls `run_structured(…)` | `enrich`, `interpret_inbound`, `match_receipt`, `parse_command` |
| **agent** | a Pydantic-AI agent (the `run_structured` seam) | the agents in `agentic/` | `propose`, `interpret`, `classify_inbound`, `propose_balance` |
| **external** | a system ynab-agent talks to | adapter import/symbol inside an activity | YNAB API, Ollama/Gemma, AgentMail, ntfy |
| **entrypoint** | what kicks workflows off | the poll starter; the AgentMail webhook → `DispatchWorkflow` | the poller, the webhook |

Whether **agent** is its own node kind or just a flag on the activity is a design
choice (§9). Default: keep agents as a *flag* on the activity plus a `reads` edge
to Ollama, so the diagram doesn't double its node count; promote to real nodes
only if you want to see agent reuse (e.g. `interpret_offer_reply` shared by the
offer and command flows).

### 2.2 Edge kinds

ynab-agent's shape lives in its edges, and several are **indirect or
cross-pipeline** — that's the whole reason a map helps.

| Edge | Meaning | Code pattern matched |
|---|---|---|
| **executes** | workflow runs an activity | `workflow.execute_activity(<mod>.<fn>, …)` |
| **dispatches** | an activity starts a (child) workflow, often signal-with-start | `temporal.start_workflow(<Wf> / "<Name>", …)` inside an `@activity.defn` |
| **signals** | an activity fires a signal at a *running* workflow | `await handle.signal(…)` / signal-with-start inside an activity |
| **awaits** | a workflow blocks on a human/peer signal | `workflow.wait_condition(<pred>)` gated on a `@workflow.signal` |
| **continues** | a loop or ledger reschedules itself | `workflow.continue_as_new(…)` (self-edge) |
| **reads/writes** | an activity touches an external system | `YnabClient` → YNAB, `MailClient` → AgentMail, `build_model`/`run_structured` → Ollama, ntfy |

The signature flow to render faithfully is the **human-in-the-loop reply path**,
which crosses pipelines:

```
AgentMail webhook ─▶ DispatchWorkflow ─executes─▶ classify_inbound (agentic)
DispatchWorkflow ─executes─▶ signal_transaction ─signals─▶ TransactionWorkflow
                                                            (which is ──awaits──▶ a human reply)
```

…and the agentic sub-step inside an activity:

```
TransactionWorkflow ─executes─▶ enrich ─(run_structured)─▶ propose ─reads─▶ Ollama
```

### 2.3 Clusters — by pipeline

ynab-agent's natural grouping is the **pipeline** (SPEC's workflow map), with one
cross-cutting hub (dispatch) and one safety lane. Six clusters:

| Pipeline | Workflows | Agentic steps | Externals |
|---|---|---|---|
| **1. Transaction** (ingest → categorize) | PollWorkflow (W1) → TransactionWorkflow (W2) | enrich · interpret · converge | YNAB, Ollama, AgentMail |
| **2. Receipt match** | DispatchWorkflow (W3) → ReceiptJoinWorkflow (W4); ReceiptLedgerWorkflow | classify_inbound · parse_receipt · match_receipt | AgentMail, Ollama |
| **3. Learning & autonomy** | RuleRegistryWorkflow (W5); AutonomyOfferWorkflow; CommandConfirmWorkflow | interpret_offer · parse_command | AgentMail, Ollama |
| **4. Budget balancing** | OverspendMonitorWorkflow (W6) → BudgetBalanceWorkflow (W7); OverspendLedgerWorkflow | propose_balance · interpret_balance_reply | YNAB, Ollama, AgentMail |
| **5. Safety / observability** | AlertLedgerWorkflow; AutoActionLedgerWorkflow; DeadmanWorkflow | — | ntfy, Temporal |
| **6. Inbound dispatch** (the hub) | DispatchWorkflow (W3) | (routes via queries + signals into 1/3/4) | AgentMail |

Pipeline 6 is the cross-link the diagram must make legible: `DispatchWorkflow` is
the single front door for every inbound email, fanning out via **signals** into
the transaction, offer, and balance workflows that sit **awaiting** a reply. Give
it its own lane in the center; the signal edges are the system's nervous system.

## 3. Static layer — the skeleton (AST walk), built from source on load

The skeleton is pure static analysis: exact, complete, deterministic. ynab-agent
already AST-walks its own `@workflow.defn` code in
[`scripts/check_determinism.py`](./scripts/check_determinism.py) — the extractor
**reuses that approach** (`ast.parse` → walk `ClassDef`/`FunctionDef` by decorator
→ inspect `Call` nodes). It lives in `ynab_agent/anatomy.py` (a normal module,
also runnable as a CLI), and the view **runs it at load time against ynab-agent's
own installed source**, so the map is always the deployed code.

Per module under `src/ynab_agent/`:

1. **Find nodes.** Every `@workflow.defn` class (with its role facet — `ledger` if
   `run()` is continue_as_new + signals/queries only) and every `@activity.defn`
   function (name, file, line, summary). Flag an activity **agentic** if its body
   calls `run_structured(…)`; record which agent in `agentic/` it drives.
2. **Edges in workflow bodies.** Match `workflow.execute_activity(<ref>, …)` →
   **executes**. Match `workflow.continue_as_new(…)` → **continues** self-edge.
   Match `@workflow.signal`-decorated handlers + the `workflow.wait_condition(…)`
   that gates on them → mark the workflow as **awaits** (a human/peer signal sink).
3. **Edges in activity bodies.** Match `temporal.start_workflow(<Wf or "Name">, …)`
   → **dispatches** (note `start_signal=` ⇒ signal-with-start). Match
   `handle.signal(…)` / signal-with-start → **signals** edge to the target
   workflow. Resolve string workflow names (`"TransactionWorkflow"`) against the
   class set. This is how the inbound reply path is reconstructed.
4. **External edges.** Detect the I/O adapters by imported symbol — `YnabClient`
   (→ YNAB), `MailClient` (→ AgentMail), `run_structured`/`build_model`
   (→ Ollama), the ntfy alert sink — and emit **reads/writes** edges. Heuristic on
   the `ynab/`, `mail/`, `agentic/` surfaces; a new adapter is invisible until
   taught (acceptable, easy to extend).
5. **Attribute clusters.** Map each workflow/activity to its pipeline. The cleanest
   source is module locality (`poll_*`/`txn`/`activities` → Transaction;
   `dispatch_*`/`receipt_*` → Receipt; `offer_*`/`command_*`/`registry_*` →
   Learning; `monitor_*`/`balance_*` → Balancing; `*_ledger_*`/`deadman_*`/`alert_*`
   → Safety) with a small hand-curated override table for the few that straddle.
6. **Return the graph** — an in-memory object. The JSON below is its serialized
   shape (the API response), *not* a file in the repo.

```jsonc
{
  "generatedFrom": "<git sha>",
  "nodes": [
    { "id": "wf:TransactionWorkflow", "kind": "workflow", "role": "driver",
      "label": "TransactionWorkflow (W2)", "cluster": "transaction",
      "awaits": true, "file": "src/ynab_agent/workflow/txn_workflow.py", "line": 1 },
    { "id": "act:enrich", "kind": "activity", "agentic": true, "agent": "propose",
      "cluster": "transaction" },
    { "id": "wf:RuleRegistryWorkflow", "kind": "workflow", "role": "ledger", … },
    { "id": "ext:ollama", "kind": "external", "label": "Ollama / Gemma" }
  ],
  "edges": [
    { "from": "wf:TransactionWorkflow", "to": "act:enrich", "kind": "executes" },
    { "from": "act:enrich", "to": "ext:ollama", "kind": "reads" },
    { "from": "act:signal_transaction", "to": "wf:TransactionWorkflow", "kind": "signals" },
    { "from": "wf:RuleRegistryWorkflow", "to": "wf:RuleRegistryWorkflow", "kind": "continues" }
  ]
}
```

Node IDs are stable (`<kind>:<symbol>`) so the trace layer joins to them.

**Built from source, every load — never regenerated.** The walker finds the
`ynab_agent` package via its import path and parses the `.py` files on disk: the
same source the running image was built from. The result is cached in-process and
invalidated by file mtime, so:

- in the **deployed image** (immutable source) it parses **once** per process, and
  every load after is instant and exactly the deployed code;
- in **local dev** (run against the working tree) editing a workflow and reloading
  re-walks and shows the change — no restart, no regen step.

That's the point of doing it this way: the map *can't* go stale, because there's
nothing to keep in sync — it **is** the source. (A `python -m ynab_agent.anatomy
--json` dump exists only as an escape hatch for hosting the view without the
source on disk; a fallback, not the path.)

## 4. Dynamic layer — the heat (traces)

- **Source:** the trace store (ClickStack's `otel_traces` in this deployment; any
  OTLP-backed store works), filtered to `ServiceName = 'ynab-agent-worker'`. The
  endpoint is deployment config (env-driven), kept out of this repo.
- **Span names:** Temporal's OTel interceptor convention — `RunWorkflow:<Class>`,
  `RunActivity:<fn>`, with `StartWorkflow:` / `StartChildWorkflow:` /
  `SignalWithStartWorkflow:` for the dispatch and signal-with-start hops, and
  Pydantic-AI spans nested inside the agentic activities. **Verify the exact
  prefixes against the live store before wiring** (Temporal versions differ).
- **Per-node metrics**, over a selectable window: call count, p50/p95/max duration,
  error rate, last-seen. Map `RunActivity:match_receipt` → `act:match_receipt`,
  etc. The agentic activities will dominate latency (slow Gemma) — that contrast
  is exactly what the heat should reveal.
- **Per-edge metrics:** caller→callee frequency via a self-join of `otel_traces`
  on `SpanId = ParentSpanId`. For the **signal** and **dispatch** hops, the child
  is a *new* workflow trace (signal-with-start), so reconstruct those from the
  `Start*`/`Signal*` spans and their links rather than strict nesting.
- **Output:** `weights`, keyed by the same node/edge IDs, fused at load.

**Temporal trace caveats** (the real tax — document the handling):

- **continue-as-new** fragments the durable loops (poll, ledgers) into many short
  traces; aggregate across them.
- **Signal-with-start** and cross-workflow signals do **not** nest under the
  sender's span — they start or wake a separate workflow. Reconstruct those edges
  from `Start*Workflow:` / `Signal*` spans + span links, not `ParentSpanId` alone.
  This is the fiddliest part; budget for it.
- **Human-in-the-loop waits** mean a `TransactionWorkflow` trace can span days; its
  activity spans are scattered across that lifetime. Aggregate by node, don't
  expect one tidy waterfall.
- **Replay** can re-emit spans; dedup on `(TraceId, SpanId)`. **Sampling/retention:**
  the store holds a rolling window — label the heat as "recent," not all-time.

## 5. Fusion + the diff (where the insight is)

The static graph is the **source of truth for topology**; the trace weights only
*annotate* it. Two free insights fall out:

- **Cold nodes** — defined in code, no traffic in the window → drawn dim. (An
  optional pipeline that's idle, or a rarely-hit agent.)
- **Surprise edges** — present in traces but absent from the static graph → drawn
  in alarm color. Either the walker missed a signal/dispatch hop (teach it) or
  something is talking to something it shouldn't (a real finding — and for a money
  agent, worth knowing).

For ynab-agent specifically, the fused view should make the **agentic middle vs
the deterministic spine** legible (SPEC §0.5): a thin band of slow, hot LLM
activities (`enrich`, `interpret_inbound`, `match_receipt`, …) sitting between the
fast YNAB/AgentMail plumbing on either side, with the dispatch hub stitching the
human replies back in. That picture *is* the architecture.

## 6. Visualization

**Not Mermaid, not a force-directed hairball.** At ~14 workflows + ~50 activities
+ agents + cross-pipeline signals, Mermaid's auto-layout tangles and carries no
interactivity or heat; a physics layout drifts and re-tangles every load, which
defeats "re-find it every time." An anatomy diagram needs a **stable,
hierarchical, layered layout** you can build a memory of — especially here, where
the signal edges cross the whole graph.

**Recommended stack** (same as froot's spec, for consistency):

- **Layout: [ELK](https://github.com/kieler/elkjs) (`elkjs`)** — its layered
  (Sugiyama) algorithm fits a directed, clustered DAG with long cross-links;
  supports compound nodes (the pipeline clusters as boxes) and deterministic,
  legible flow. ELK's orthogonal edge routing keeps the cross-pipeline signal
  edges readable instead of diagonal spaghetti.
- **Render + interaction: [Cytoscape.js](https://js.cytoscape.org/)** with the ELK
  layout extension — mature, loads from a single script tag (no bundler; fits an
  in-worker page), compound nodes for clusters, rich events/styling/pan-zoom.
- **Alternative** for richer node cards (live stats per node): **React Flow
  (`@xyflow/react`) + elkjs**, at the cost of a Vite/React build. Only if the card
  UI earns the toolchain.
- **Ruled out:** Mermaid (static, poor layout at scale, no heat/interaction); raw
  `d3-force` (hairball, unstable). D3 stays useful for color scales and the legend.

**Layout shape:** entrypoints (poller, webhook) at the top → the dispatch hub in a
central lane → the pipeline lanes (transaction, receipt, learning, balancing) with
their workflows over their activities → agents/externals at the periphery; the
safety ledgers in a base lane. **Edge styling carries the most meaning here:**

| Channel | Encodes |
|---|---|
| node shape | kind (workflow = rounded rect, ledger = stacked rect, activity = pill, agent = diamond, external = cylinder) |
| node size | call count (trace) |
| node color | error rate / latency heat; agentic activities tinted warm; cold = dim |
| node badge | "awaits" sink (a small clock/pause glyph for human-in-the-loop) |
| edge thickness | call frequency (trace) |
| edge style | executes = solid · dispatches = solid+arrow · **signals = dashed accent (the nervous system)** · awaits = the signal's target glyph · continues = looped dashed · reads/writes = dotted to external |
| cluster box | pipeline |

**Interactions:** hover → tooltip (count, p50/p95, error rate, `file:line`,
agent name); click node → focus its neighborhood (and, for a workflow, highlight
who signals it); filter by pipeline; isolate the **signal graph** (just the
dispatch/await edges) to see the message nervous system alone; time-window
selector; toggle skeleton-only vs heat; search; "show surprises." Always-on
**legend**.

## 7. Pipeline + delivery

The view is served **in-process by the worker**, where ynab-agent's source already
lives — the in-worker ops dashboard (SPEC §15) runs there, so this rides
alongside it. Per request (or refresh):

1. **Skeleton:** build the static graph by AST-walking ynab-agent's installed
   source (§3) — parsed once per process, mtime-invalidated, so it's always the
   running code with no generation step and nothing to keep in sync.
2. **Heat:** query the trace store for the window, build the per-node / per-edge
   weights (§4).
3. **Fuse + serve:** merge by ID and return one self-contained page (the graph +
   the Cytoscape/ELK render). A small read-only handler, no build toolchain.

Because the skeleton is built from source at load, **nothing needs updating when
the code changes**: redeploy (new image → new source) and the map follows;
locally, just reload. Only the trace overlay depends on the deployment — the
trace-store endpoint, refresh cadence, auth, and hosting are config (env-driven)
and live with the workspace infra, not in this repo.

## 8. Phasing

- **P1 — skeleton.** The in-process AST walk (`ynab_agent/anatomy.py`, built from
  source on load) + a layered ELK/Cytoscape render clustered by pipeline, served
  from the worker. Ship it; eyeball whether the structure reads. Already
  always-fresh. (Answers "where does it live in the tree.")
- **P2 — heat.** Trace overlay: node/edge weights, the agentic-vs-spine contrast,
  hover stats. Includes the fiddly signal/dispatch-edge reconstruction (§4).
- **P3 — polish.** The signal-graph isolate view, the "awaits" badges, cold-node
  dimming, surprise-edge detection, window selector, search, focus mode,
  deployment behind the workspace's auth.

## 9. Risks & open questions

- **Signal / dispatch edges in traces** are the main tax — they don't nest under
  the sender, so they need `Start*`/`Signal*` span + link reconstruction (§4).
  The static layer gets them cleanly from the AST; the *heat* on those edges is the
  hard part.
- **Agent as node vs flag** (§2.1): flag-by-default; promote to nodes only if agent
  reuse is worth seeing.
- **String workflow names** (`temporal.start_workflow("TransactionWorkflow", …)`)
  must resolve to the class set; a typo'd or dynamic name is an unresolved edge —
  surface it rather than dropping it.
- **Cluster attribution** leans on module locality + a small override table; the
  few straddling activities (shared offer/command interpret) need a curated call.
- **Build-from-source cost** is sub-second and cached per process, negligible — but
  it means the view runs where the source is (the worker image), which is why it's
  in-process, not a source-less static site. The `--json` dump is the escape hatch.
- **Span-name prefixes** must be confirmed against the live store before wiring.

## 10. Non-goals

- Not a live per-execution trace viewer (HyperDX covers that).
- Not a replacement for the ops dashboard, the gate, or the safety ledgers.
- Not cross-service (ynab-agent and froot don't call each other; a future "atlas"
  could place their two maps side by side, but they share no edges).
- Not real-time streaming; a periodic heat refresh is plenty.
