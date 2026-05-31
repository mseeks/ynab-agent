# YNAB Agent — developer terrain.
# Short, single-purpose targets: each is one tool, easy to read and approve.

.PHONY: sync fmt fmt-check lint type test check loop-type-debt loop-comment-debt

SCOPE ?=

# Install/refresh the dev environment (uv-managed venv, dev + loops extras).
sync:
	uv sync --extra dev --extra loops

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
