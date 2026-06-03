# YNAB Agent — developer terrain.
# Short, single-purpose targets: each is one tool, easy to read and approve.

.PHONY: sync fmt fmt-check lint type test check loop-type-debt loop-comment-debt loop-debug-cruft loop-doc-coherence loop-duplicated-constant loop-dead-code loop-test-backfill loop-determinism loop-sandbox-imports loop-secret-leak loop-derived-state loop-model-seam loop-activity-retry loop-frozen-mutability loop-pure-core-isolation loop-variant-exhaustiveness loop-spec-citation

SCOPE ?=

# Install/refresh the dev env (uv venv: dev, loops, ai, mail, ynab, webhook, otel).
sync:
	uv sync --extra dev --extra loops --extra ai --extra mail --extra ynab --extra webhook --extra otel

# Auto-format (ruff formatter) and fix lint where safe.
fmt:
	uv run ruff format src tests agents
	uv run ruff check --fix src tests agents

# Verify formatting without writing (CI-friendly).
fmt-check:
	uv run ruff format --check src tests agents

# Lint only (no fixes).
lint:
	uv run ruff check src tests agents

# Strict type check (the DDD safety net; covers src, tests, and the loops).
type:
	uv run mypy

# Run the test suite with coverage.
test:
	uv run pytest

# The full gate: format check, lint, types, tests. Keep this green.
check: fmt-check lint type test

# Run the type-debt loop (read-only). Optional scope: make loop-type-debt SCOPE=src
loop-type-debt:
	uv run python -m agents.type_debt $(SCOPE)

# Run the comment-debt loop (read-only). Optional: make loop-comment-debt SCOPE=src
loop-comment-debt:
	uv run python -m agents.comment_debt $(SCOPE)

# Run the debug-cruft loop (read-only). Optional: make loop-debug-cruft SCOPE=src
loop-debug-cruft:
	uv run python -m agents.debug_cruft $(SCOPE)

# Run the doc-coherence loop (read-only). Scope is a .md file or dir; default: .
loop-doc-coherence:
	uv run python -m agents.doc_coherence $(SCOPE)

# Run the duplicated-constant loop (read-only). Optional scope (a path); default: src
loop-duplicated-constant:
	uv run python -m agents.duplicated_constant $(SCOPE)

# Run the dead-code loop (read-only). Optional scope (a path); default: src
loop-dead-code:
	uv run python -m agents.dead_code $(SCOPE)

# Run the test-backfill loop (read-only). Best run per package; default: src
loop-test-backfill:
	uv run python -m agents.test_backfill $(SCOPE)

# Run the determinism loop (read-only). Scans workflow files; default: src
loop-determinism:
	uv run python -m agents.determinism $(SCOPE)

# Run the sandbox-imports loop (read-only). Scans workflow/activity files; src
loop-sandbox-imports:
	uv run python -m agents.sandbox_imports $(SCOPE)

# Run the secret-leak loop (read-only). Scans for hardcoded credentials; src
loop-secret-leak:
	uv run python -m agents.secret_leak $(SCOPE)

# Run the derived-state loop (read-only). Scans for persistent-store smells; src
loop-derived-state:
	uv run python -m agents.derived_state $(SCOPE)

# Run the model-seam loop (read-only). Flags agent calls that bypass run_structured; src
loop-model-seam:
	uv run python -m agents.model_seam $(SCOPE)

# Run the activity-retry loop (read-only). Flags execute_activity calls with no retry_policy; src
loop-activity-retry:
	uv run python -m agents.activity_retry $(SCOPE)

# Run the frozen-mutability loop (read-only). Flags mutable fields on frozen models; src
loop-frozen-mutability:
	uv run python -m agents.frozen_mutability $(SCOPE)

# Run the pure-core-isolation loop (read-only). Flags I/O imports in the pure core; src
loop-pure-core-isolation:
	uv run python -m agents.pure_core_isolation $(SCOPE)

# Run the variant-exhaustiveness loop (read-only). Flags undispatched union members; src
loop-variant-exhaustiveness:
	uv run python -m agents.variant_exhaustiveness $(SCOPE)

# Run the spec-citation loop (read-only). Flags dangling SPEC §refs in code; src
loop-spec-citation:
	uv run python -m agents.spec_citation $(SCOPE)
