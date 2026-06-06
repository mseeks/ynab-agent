# YNAB Agent

A durable AI agent that triages, categorizes, splits, memos, and approves YNAB
transactions, driven by per-transaction email threads. It runs as a
deterministic **Temporal spine** (when to act, did-it-happen, the hard safety
floor) wrapping an agentic **Pydantic AI middle** (what the category/split/memo
is). See [`SPEC.md`](./SPEC.md) for the full design.

## Architecture

The seam is **who chooses**: the spine decides *when* and *whether*; the model
decides *what* and *how it's worded*. Everything is built inside-out — a pure,
immutable domain core with illegal states made unrepresentable, then pure
policy, then thin Temporal workflows whose side effects are activities, then the
real model/email/budget I/O behind those activity ports.

```
inbound email ─► W3 dispatch ─► { W2 lifecycle, W4 receipt join }
                                      │            │
                                      ▼            ▼
   W1 ingest poller ─► W2 ◄───── policy: floor · gate · resolve · converge
                        │                          ▲
                        ▼                          │
                  W5 rule learning ───────────► the gate (earned autonomy)
   W6 overspend monitor ─► W7 budget balancer (propose → confirm by NL → apply)
```

| Workflow | Role |
|---|---|
| **W1** ingest poller | discover new YNAB transactions, address them, signal W2 |
| **W2** transaction lifecycle | the durable per-transaction state machine (propose → gate → commit→verify → archive; reopens on late replies) |
| **W3** inbound dispatcher | a signed AgentMail webhook → reply / receipt / command / quarantine |
| **W4** receipt join | match a forwarded receipt to a transaction (act once, ask once) |
| **W5** rule learning | confirm/correct → rules + trust (the memory that earns autonomy) |
| **W6 / W7** budget guards | overspend monitor (notify) + budget balancer (model proposes coverage options, a natural-language reply applies them) |

The **agentic middle** is a set of Pydantic AI agents over a local Ollama running
**Gemma 4** (the SPEC §0.5 path): `enrich` (propose a category — and, on an
auto-apply, an independent clean-context *safety review* that can only veto),
`interpret` (read a reply's intent), `match` (join a receipt), `classify` (triage
inbound), `converge` (interpret a revision), `offer` (read a yes/no to a
proactive autonomy offer), and `balance` (propose budget-coverage options and
read a coverage reply — given a calculator tool so its arithmetic is exact). Each
produces domain-typed structured output and lives in the `agentic` package, never
imported into a Temporal sandbox.

## Layout

```
src/ynab_agent/
  domain/      pure, frozen DDD core (illegal states unrepresentable)
  policy/      floor · autonomy gate · allocation resolve · converge
  ingest/ dispatch/ join/ learn/ budget/ audit/   the pure spines of W1,W3,W4,W5,W6/7,§9
  workflow/    Temporal workflows + activity ports (the I/O boundary)
  agentic/     Pydantic AI agents (the model middle, Ollama/Gemma)
  mail/        AgentMail client (real email)
  ynab/        YNAB REST client (snapshots, commits, budget reads)
  dashboard/   in-worker ops dashboard (Temporal/YNAB/ClickHouse/AgentMail/GitHub)
  settings.py  deployment config (pydantic-settings, .env)
  worker.py    the Temporal worker entrypoint
tests/         unit + property-based (hypothesis) + time-skipping workflow tests
agents/        Many Hands Engineering loops (agentic linters) — see agents/README.md
SPEC.md        the design spec
```

## Status

The full system is built and tested inside-out. Every workflow (W1–W7), the
policy layer, rule learning, and the audit log are pure and exhaustively tested;
the model agents are real (offline-tested via `TestModel`, plus opt-in live
Gemma smokes); **AgentMail sending is live**; the **YNAB REST client** is built
and unit-tested with live calls gated on `YNAB_API_KEY`. The activity ports are
progressively wired to the real clients (the YNAB reads are connected); the rest
run mock implementations in the tests.

The **autonomy lifecycle is complete** (SPEC §14.7 3b / §14.8): a learned rule
*earns* eligibility (a deliberately high `K`), the agent *proactively offers* to
auto-handle the payee (a one-time email; a yes blesses it), a clean-context model
*safety review* vetoes any auto-apply it finds doubtful, and autonomy is *revoked*
— the rule demoted back to Observe — on an explicit correction or a silent
manual edit in YNAB.

## Running

```bash
make sync     # create/refresh the dev environment (all extras)
make fmt      # format + autofix
make check    # the full gate: format, lint, types, tests — keep it green
make test     # the test suite with coverage
```

The worker needs a reachable Temporal server, and the clients read their keys
from the environment (never the repo):

```bash
export YNAB_API_KEY=…           export AGENTMAIL_API_KEY=…
export YNAB_AGENT_INBOX=…       export YNAB_AGENT_OWNERS=a@x.com,b@x.com
python -m ynab_agent.worker     # connects to localhost:7233, queue "ynab-agent"
```

The worker also hosts a private, read-only **ops dashboard** (SPEC §15) on
`:8080`, reached over `kubectl port-forward` — one page that derives the whole
system's state live from Temporal, YNAB, ClickHouse, AgentMail, and GitHub
(stored nowhere). Preview it against live data with `python -m
ynab_agent.dashboard` (point `TEMPORAL_*` at a port-forwarded frontend).

Opt-in live checks (skipped by default): `YNAB_AGENT_LIVE_OLLAMA=1` runs the live
Gemma smokes, `YNAB_AGENT_LIVE_EMAIL=1` sends a real AgentMail message, and a set
`YNAB_API_KEY` enables the live YNAB smoke.

## Loops

Development runs on **Many Hands Engineering loops** — small read-only agentic
linters (a deterministic signal + a locked-down Claude pass that maps each hit
into a cite-or-omit three-bucket verdict). Ten of them, from `type-debt` to the
Temporal-specific `determinism` and `sandbox-imports` guards. See
[`agents/README.md`](./agents/README.md).

## Toolchain

Python 3.13, managed with [uv](https://docs.astral.sh/uv/). Strict `mypy` (with
the Pydantic plugin) is the type safety net; `ruff` enforces the Google Python
style guide (including Google-style docstrings); `pytest` + `hypothesis` cover
behavior and invariants. Temporal's time-skipping test server drives the
workflows; Pydantic AI's `TestModel` drives the agents offline.

## Design principles

- **Ramp from cautious.** Autonomy is earned per-payee; no global trust switch.
- **A thin deterministic floor, then trust the model.** Caps, a circuit breaker,
  and idempotency bound catastrophe; above that, the model interprets.
- **Decisions are never final.** A late reply can reopen and rewrite an approved
  transaction; a correction is the highest-signal event in the system.
- **Rules, not raw confidence, authorize autonomy.** A human-blessed or
  K-confirmed rule grants auto-apply; model confidence only shapes wording.
- **Make illegal states unrepresentable.** The domain types carry the rules.
