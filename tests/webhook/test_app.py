"""Tests for the W3 inbound webhook receiver (SPEC §5)."""

from __future__ import annotations

import base64
import datetime
import json
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient
from svix.webhooks import Webhook

from ynab_agent.dispatch.classify import InboundMessage
from ynab_agent.domain.ids import MessageId
from ynab_agent.webhook.app import (
    _WireMessage,
    create_app,
    start_dispatch,
    to_inbound,
)

if TYPE_CHECKING:
    import pytest

_SECRET = "whsec_" + base64.b64encode(b"a-test-webhook-secret-32").decode()
_ALLOW = frozenset({"matthew@example.com"})


def _signed(body: str) -> dict[str, str]:
    """Sign a body the way AgentMail/Svix would, for a valid request.

    Svix verify enforces a 5-minute timestamp tolerance, so the signature has to
    be stamped with the real wall-clock time, not a frozen one.
    """
    when = datetime.datetime.now(datetime.UTC)
    msg_id = "msg_test"
    signature = Webhook(_SECRET).sign(msg_id, when, body)
    return {
        "svix-id": msg_id,
        "svix-timestamp": str(int(when.timestamp())),
        "svix-signature": signature,
        "content-type": "application/json",
    }


def _event(event_type: str = "message.received") -> str:
    return json.dumps(
        {
            "type": event_type,
            "message": {
                "message_id": "m1",
                "from": "matthew@example.com",
                "subject": "[YNAB] $4.50",
                "text": "ok",
                "thread_id": "thr1",
            },
        }
    )


def test_to_inbound_maps_the_message() -> None:
    message = _WireMessage.model_validate(
        {
            "message_id": "m1",
            "from": "a@x.com",
            "subject": "s",
            "text": "hi",
            "thread_id": "t1",
        }
    )
    inbound = to_inbound(message, verified=True)
    assert inbound.from_address == "a@x.com"
    assert inbound.body == "hi"
    assert inbound.thread_id == "t1"
    assert inbound.signature_verified is True


def _app_with_capture(
    monkeypatch: pytest.MonkeyPatch, captured: list[InboundMessage]
) -> TestClient:
    async def fake_start(
        client: object,
        inbound: InboundMessage,
        *,
        allowlist: frozenset[str],
        task_queue: str,
    ) -> bool:
        captured.append(inbound)
        return True

    monkeypatch.setattr(
        "ynab_agent.webhook.app.start_dispatch", fake_start
    )
    app = create_app(
        webhook_secret=_SECRET, allowlist=_ALLOW, task_queue="ynab-agent"
    )
    app.state.temporal = object()  # sentinel; fake_start ignores it
    return TestClient(app)


def test_valid_webhook_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[InboundMessage] = []
    body = _event()
    with _app_with_capture(monkeypatch, captured) as client:
        response = client.post(
            "/webhooks/agentmail", content=body, headers=_signed(body)
        )
    assert response.status_code == 200
    assert response.json()["status"] == "dispatched"
    assert captured[0].from_address == "matthew@example.com"
    assert captured[0].signature_verified is True


def test_bad_signature_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[InboundMessage] = []
    with _app_with_capture(monkeypatch, captured) as client:
        response = client.post(
            "/webhooks/agentmail",
            content=_event(),
            headers={
                "svix-id": "msg_test",
                "svix-timestamp": "1780000000",
                "svix-signature": "v1,not-a-real-signature",
            },
        )
    assert response.status_code == 401
    assert captured == []


def test_non_received_event_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[InboundMessage] = []
    body = _event("message.received.spam")
    with _app_with_capture(monkeypatch, captured) as client:
        response = client.post(
            "/webhooks/agentmail", content=body, headers=_signed(body)
        )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert captured == []


async def test_start_dispatch_is_idempotent_on_retry() -> None:
    from temporalio.testing import WorkflowEnvironment

    from ynab_agent.workflow.runtime import DATA_CONVERTER

    inbound = InboundMessage(
        message_id=MessageId("m1"),
        from_address="matthew@example.com",
        subject="[YNAB] $4.50",
        body="ok",
        signature_verified=True,
    )
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        first = await start_dispatch(
            env.client, inbound, allowlist=_ALLOW, task_queue="ynab-agent"
        )
        second = await start_dispatch(
            env.client, inbound, allowlist=_ALLOW, task_queue="ynab-agent"
        )
    assert first is True
    assert second is False  # REJECT_DUPLICATE dedups the webhook retry
