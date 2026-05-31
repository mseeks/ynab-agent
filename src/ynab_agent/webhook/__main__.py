"""Run the inbound webhook receiver (SPEC §5).

    python -m ynab_agent.webhook        # binds 0.0.0.0:$PORT (default 8080)

Config is from the environment: ``AGENTMAIL_WEBHOOK_SECRET`` (the Svix secret),
``TEMPORAL_HOST`` / ``TEMPORAL_NAMESPACE`` / ``TEMPORAL_TASK_QUEUE``, and the
recipient allow-list via :class:`~ynab_agent.settings.Settings` (the owners).
"""

from __future__ import annotations

import os


def main() -> None:
    """Console entrypoint: serve the webhook app from the environment."""
    import uvicorn

    from ynab_agent.webhook.app import create_app

    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )


if __name__ == "__main__":
    main()
