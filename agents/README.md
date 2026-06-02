# Loops

Many Hands Engineering loops for the YNAB agent. A loop is a small, reversible
cycle — *signal → action → verification → report* — run as part of the
development cycle. Each is a deterministic signal (computed in plain Python)
plus a locked-down, **read-only** Claude Agent SDK pass that verifies each hit
in context and emits a strict, **cite-or-omit** map. Loops propose; you address.

We are at framework Stage 1: one loop, closed end-to-end, as the template. More
loops of the *same shape* come next — we resist broadening any single one.

## Loops

| Loop | Signal | Output |
|---|---|---|
| `type-debt` | regex sweep for `Any`, `cast(`, `# type: ignore` / `# mypy:` / `# pyright:` | three-bucket map: *Tightenable now* / *Legitimately needed* / *Judgment-heavy* |
| `comment-debt` | regex sweep for `TODO` / `FIXME` / `HACK` / `XXX` | three-bucket map: *Action now* / *Legitimately kept* / *Judgment-heavy* |
| `debug-cruft` | regex sweep for `print(` / `breakpoint()` / `pdb` / pytest `skip`/`xfail` | three-bucket map: *Delete now* / *Intentional* / *Stale skip — re-enable* |
| `doc-coherence` | Markdown scan for broken links, missing file paths, removed `make` targets (+ agent finds semantic drift vs code) | three-bucket map: *Drift confirmed* / *Not drift* / *Judgment-heavy* |
| `duplicated-constant` | scan for the same numeric literal re-typed on a rule line (comparison / `timedelta` / `Field` bound / `Money`) at 2+ sites | three-bucket map: *Centralise now* / *Legitimately repeated* / *Judgment-heavy* |
| `dead-code` | AST scan for top-level defs/classes whose name is never referenced (as a word) in `src/` or `tests/` | three-bucket map: *Delete now* / *Reachable* (framework false positive) / *Judgment-heavy* |
| `test-backfill` | AST scan for *public* symbols used in `src/` but named in no test (the tests-axis of dead-code) | three-bucket map: *Worth testing* (cases proposed) / *Skip* / *Worth testing but hard* |
| `determinism` | scan `@workflow.defn` files for bare nondeterminism (`datetime.now(`, `random.`, `uuid.`, `asyncio.sleep(`) that must use `workflow.*` | three-bucket map: *Hazard* (replacement named) / *Safe* (in an activity / a type / a comment) / *Judgment-heavy* |
| `sandbox-imports` | AST scan of `@workflow.defn`/`@activity.defn` files for module-level imports of forbidden stacks (`pydantic_ai`, `agentmail`, `agentic`, `mail`) that would enter the Temporal sandbox | three-bucket map: *Hazard* (move lazy / TYPE_CHECKING) / *Safe* (already lazy / type-only) / *Judgment-heavy* |
| `secret-leak` | scan for a secret-named binding (`api_key`/`token`/`password`…) to a 16+ char string literal (env reads excluded) | three-bucket map: *Secret* (rotate + move to env) / *Not a secret* (placeholder / fixture) / *Judgment-heavy*; values masked |
| `derived-state` | regex sweep for a persistent-store smell (a DB/cache/ORM driver import, an on-disk kv/pickle store, a `*_DATABASE_URL`-style env, or a local `store`/`persistence`/`db` import) | three-bucket map: *Violates derived-state* (move to Temporal state / search attribute, or derive from YNAB/AgentMail) / *Not a store* (transient / fixture / config read) / *Judgment-heavy* |
| `model-seam` | location-aware sweep for an agent invocation that bypasses `run_structured` (a `.run(` inside `agentic/` outside `model.py`, an `Agent(` built outside `agentic/`, or an imported private `_AGENT`) | three-bucket map: *Bypass* (route through `run_structured`) / *Safe* (the seam itself / a comment / a `TYPE_CHECKING` annotation / a test) / *Judgment-heavy* |
| `activity-retry` | AST scan for `workflow.execute_activity` / `execute_local_activity` calls that pass no `retry_policy` (so they inherit Temporal's unbounded-retry default) | three-bucket map: *No policy* (a write / agentic activity that must bound retries or mark errors non-retryable) / *Acceptable* (an idempotent, retry-safe read) / *Judgment-heavy* |
| `frozen-mutability` | base-class-aware AST scan: a `Frozen` subclass (closed transitively) with a field typed `list` / `dict` / `set` (mutable in place, incl. under `\| None` / `Optional[...]`) | three-bucket map: *Mutable* (use `tuple` / `Mapping` / `frozenset`) / *Immutable* (already immutable / `ClassVar` / not a frozen model) / *Judgment-heavy* |

File discovery (`lib.iter_python_files`) and the locked-down agent pass
(`lib.run_loop`) are shared; the regex-sweep loops (`type-debt`, `comment-debt`,
`debug-cruft`) add only a marker set plus a system prompt, while the rest bring a
purpose-built scan (a Markdown ref-check, a literal-dedup, an AST reference
count, a workflow-hazard sweep) — the framework's "replicate the template" shape
(Stage 2).

The first six loops are Python ports of Revisionist's; `determinism`,
`sandbox-imports`, and `derived-state` are *net-new* — project-specific guards
(`derived-state` enforces the placement decision that durable state lives only in
Temporal or is derived from YNAB/AgentMail, never an external store);
`determinism`/`sandbox-imports` are Temporal-specific guards (replay-determinism
and the sandbox import graph) with no Revisionist analogue. `model-seam`,
`activity-retry`, and `frozen-mutability` are the same kind of project-specific
guard: `model-seam` keeps every agent invocation on the `run_structured` seam
(NativeOutput + `reasoning_effort:"none"`) that fixed the Ollama #15288 outage,
`activity-retry` flags activities that inherit Temporal's unbounded-retry default
(a deterministic failure then spins forever), and `frozen-mutability` enforces
that the frozen domain core carries only immutable fields.

## Running

```bash
make sync                          # installs the loops extra (claude-agent-sdk)
make loop-type-debt                # scope defaults to src/
make loop-comment-debt SCOPE=src/ynab_agent/domain
# or directly:
uv run python -m agents.type_debt [scope]
uv run python -m agents.comment_debt [scope]
uv run python -m agents.debug_cruft [scope]
uv run python -m agents.doc_coherence [scope]
uv run python -m agents.duplicated_constant [scope]
uv run python -m agents.dead_code [scope]
uv run python -m agents.test_backfill [scope]   # best run per package
uv run python -m agents.determinism [scope]
uv run python -m agents.sandbox_imports [scope]
uv run python -m agents.secret_leak [scope]
uv run python -m agents.derived_state [scope]
uv run python -m agents.model_seam [scope]
uv run python -m agents.activity_retry [scope]
uv run python -m agents.frozen_mutability [scope]
```

## Auth & safety

- **Auth:** Claude subscription OAuth (`CLAUDE_CODE_OAUTH_TOKEN`), never an API
  key. Run `claude setup-token` once and put the token in `agents/.env` (see
  `.env.example`); a worktree reuses a sibling project's token automatically.
- **Read-only:** the agent gets only `Read`/`Grep`/`Glob`. `permission_mode`
  is `dontAsk`, so anything off the allowlist is denied — it cannot write, run
  shell, or read `.env`. Settings are hermetic (`setting_sources=[]`).
- **Budget:** a hard `max_budget_usd` cap per run (default $1).
- **Idempotency:** there is no findings cache; each run is fresh against the
  live tree. "Addressed" findings are the ones you fixed — the next run no
  longer surfaces them.
