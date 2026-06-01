"""W3 · the inbound webhook receiver (SPEC §5).

A tiny HTTP front door, deployed alongside the worker. AgentMail POSTs a
Svix-signed event here when an email arrives; this app verifies the signature
(proving the request really came from AgentMail — the ``signature_verified``
provenance check, SPEC §0.6), maps the message onto the domain
``InboundMessage``, and starts a ``DispatchWorkflow`` on Temporal. The
*dispatcher* then does the routing (reply / receipt / command / quarantine);
this only authenticates the transport and hands off.

AgentMail already authenticates the *sender* (SPF/DKIM/DMARC) and surfaces it as
the event type, so only ``message.received`` is acted on — ``*.unauthenticated``
/ ``*.spam`` / ``*.blocked`` are ignored here. The Temporal client lives on
``app.state`` so tests inject one without a real connection.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ynab_agent.dispatch.classify import InboundMessage
from ynab_agent.domain.ids import MessageId, ThreadId
from ynab_agent.workflow.dispatch_types import DispatchParams
from ynab_agent.workflow.dispatch_workflow import DispatchWorkflow

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from temporalio.client import Client

WEBHOOK_SECRET_ENV = "AGENTMAIL_WEBHOOK_SECRET"
_RECEIVED = "message.received"


class _WireMessage(BaseModel):
    """The message fields we read from an AgentMail webhook (loose)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message_id: str
    from_address: str = Field(alias="from")
    subject: str = ""
    text: str | None = None
    # The new reply text with the quoted history stripped — what the human
    # actually wrote. Preferred over ``text`` so the interpreter isn't fed the
    # whole quoted proposal back.
    extracted_text: str | None = None
    thread_id: str | None = None


class _WebhookPayload(BaseModel):
    """An AgentMail webhook event — only the parts we use.

    The Svix envelope's top-level ``type`` is always the literal ``"event"``;
    the actual event name (``message.received`` and its ``.spam`` / ``.blocked``
    / ``.unauthenticated`` variants) is in ``event_type``. We act only on an
    exact ``message.received``.
    """

    model_config = ConfigDict(extra="ignore")

    event_type: str = ""
    message: _WireMessage | None = None


def to_inbound(message: _WireMessage, *, verified: bool) -> InboundMessage:
    """Map a verified webhook message onto the domain InboundMessage."""
    return InboundMessage(
        message_id=MessageId(message.message_id),
        from_address=message.from_address,
        subject=message.subject,
        body=message.extracted_text or message.text or "",
        thread_id=ThreadId(message.thread_id) if message.thread_id else None,
        signature_verified=verified,
    )


def verify(body: bytes, headers: Mapping[str, str], secret: str) -> object:
    """Verify an AgentMail (Svix) webhook and return its parsed payload.

    Raises ``svix.webhooks.WebhookVerificationError`` if the signature is bad.
    """
    from svix.webhooks import Webhook

    return Webhook(secret).verify(body, dict(headers))


async def start_dispatch(
    client: Client,
    inbound: InboundMessage,
    *,
    allowlist: frozenset[str],
    task_queue: str,
) -> bool:
    """Start a DispatchWorkflow for one message; idempotent (SPEC §5).

    The workflow id is derived from the message id, and the id-reuse policy
    rejects duplicates (running *or* completed), so an AgentMail webhook retry
    is a no-op rather than a re-dispatch. Returns ``True`` if newly started,
    ``False`` if it had already been dispatched.
    """
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    try:
        await client.start_workflow(
            DispatchWorkflow.run,
            DispatchParams(message=inbound, allowlist=allowlist),
            id=f"dispatch-{inbound.message_id}",
            task_queue=task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        return False
    return True


def _allowlist_from_env() -> frozenset[str]:
    from ynab_agent.settings import Settings

    return frozenset(address.lower() for address in Settings().owners)


async def _connect() -> Client:
    from temporalio.client import Client
    from temporalio.contrib.pydantic import pydantic_data_converter

    from ynab_agent.telemetry import metrics_runtime, tracing_interceptors

    return await Client.connect(
        os.environ.get("TEMPORAL_HOST", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        data_converter=pydantic_data_converter,
        interceptors=tracing_interceptors(),
        runtime=metrics_runtime(),
    )


def create_app(
    *,
    webhook_secret: str | None = None,
    allowlist: frozenset[str] | None = None,
    task_queue: str | None = None,
) -> FastAPI:
    """Build the webhook app.

    Production reads its config from the environment; tests pass it in and set
    ``app.state.temporal`` before startup.
    """
    secret = (
        webhook_secret
        if webhook_secret is not None
        else os.environ.get(WEBHOOK_SECRET_ENV)
    )
    queue = task_queue or os.environ.get("TEMPORAL_TASK_QUEUE", "ynab-agent")
    allow = allowlist if allowlist is not None else _allowlist_from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not hasattr(app.state, "temporal"):
            app.state.temporal = await _connect()
        yield
        # uvicorn fires this on SIGTERM; flush buffered spans before exit.
        from ynab_agent.telemetry import shutdown_tracing

        shutdown_tracing()

    app = FastAPI(lifespan=lifespan, title="ynab-agent webhook")

    from ynab_agent.telemetry import instrument_fastapi, setup_tracing

    setup_tracing("ynab-agent-webhook")
    instrument_fastapi(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/agentmail")
    async def agentmail_webhook(request: Request) -> dict[str, str]:
        if not secret:
            raise HTTPException(
                status_code=500, detail="webhook secret not configured"
            )
        body = await request.body()
        from svix.webhooks import WebhookVerificationError

        try:
            payload = verify(body, request.headers, secret)
        except WebhookVerificationError as err:
            raise HTTPException(
                status_code=401, detail="invalid signature"
            ) from err

        event = _WebhookPayload.model_validate(payload)
        if event.event_type != _RECEIVED or event.message is None:
            return {"status": "ignored"}

        inbound = to_inbound(event.message, verified=True)
        started = await start_dispatch(
            request.app.state.temporal,
            inbound,
            allowlist=allow,
            task_queue=queue,
        )
        return {"status": "dispatched" if started else "duplicate"}

    return app
