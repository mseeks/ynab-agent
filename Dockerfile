# The Temporal worker image (SPEC §0.5). Built and pushed to ghcr.io by CI;
# deployed to the DOKS cluster. The model/mail/ynab clients read their keys from
# the environment at runtime (never baked in); the worker connects to Temporal.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Resolve dependencies from the lockfile, with only the runtime extras the image
# needs (the agentic, mail, YNAB, and webhook stacks) — not the dev/loops
# tooling. One image, two entrypoints: the worker (default, below) and the W3
# webhook receiver, which the webhook Deployment selects via a `command:`
# override (python -m ynab_agent.webhook).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-editable \
    --extra ai --extra mail --extra ynab --extra webhook --extra otel

ENTRYPOINT ["uv", "run", "--no-sync", "python", "-m", "ynab_agent.worker"]
