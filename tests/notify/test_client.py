"""Tests for the ntfy push client (SPEC §13).

The client puts an alert on the wire as a single POST to ``{base}/{topic}`` with
the title/priority/tags in headers and the body as the payload. These exercise
that mapping with a mock transport (no network) plus the env-config guard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from ynab_agent.notify.client import (
    Notification,
    NotifyClient,
    _HttpxBackend,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_from_env_requires_a_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    with pytest.raises(RuntimeError, match="NTFY_TOPIC"):
        NotifyClient.from_env()


def test_publishes_title_priority_tags_and_body() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["title"] = request.headers.get("Title")
        captured["priority"] = request.headers.get("Priority")
        captured["tags"] = request.headers.get("Tags")
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = request.content
        return httpx.Response(200)

    backend = _HttpxBackend(
        _client(handler),
        base_url="https://ntfy.sh",
        topic="secret-topic",
        token=None,
    )
    NotifyClient(backend).notify(
        Notification(
            title="ynab-agent: enrich failed",
            body="Blue Bottle (txn t1)",
            priority="high",
            tags=("rotating_light", "moneybag"),
        )
    )

    assert captured["url"] == "https://ntfy.sh/secret-topic"
    assert captured["title"] == "ynab-agent: enrich failed"
    assert captured["priority"] == "high"
    assert captured["tags"] == "rotating_light,moneybag"
    assert captured["auth"] is None
    assert captured["body"] == b"Blue Bottle (txn t1)"


def test_includes_bearer_token_for_a_protected_topic() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200)

    backend = _HttpxBackend(
        _client(handler),
        base_url="https://ntfy.example.com",
        topic="t",
        token="tk_secret",
    )
    NotifyClient(backend).notify(Notification(title="t", body="b"))

    assert captured["auth"] == "Bearer tk_secret"


def test_raises_on_a_non_2xx_response() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    backend = _HttpxBackend(
        _client(handler), base_url="https://ntfy.sh", topic="t", token=None
    )
    with pytest.raises(httpx.HTTPStatusError):
        NotifyClient(backend).notify(Notification(title="t", body="b"))
