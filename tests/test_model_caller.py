"""The model client tags each request with its caller (``X-Model-Client``).

The proxy attributes traffic per caller from this header, so the tagging is the
load-bearing new behavior: the bare app name outside an activity, and the per
-request stamp from the shared client's event hook.
"""

from __future__ import annotations

import asyncio

import httpx

from ynab_agent.agentic import model


def test_caller_tag_outside_activity() -> None:
    """Outside an activity context, the tag is the bare app name."""
    assert model._caller_tag() == "ynab-agent"


def test_stamp_caller_sets_header() -> None:
    """The event hook stamps the current caller onto the request headers."""
    token = model._caller.set("ynab-agent/interpret")
    try:
        request = httpx.Request("POST", "http://ollama.llm/v1/chat/completions")
        asyncio.run(model._stamp_caller(request))
        assert request.headers["X-Model-Client"] == "ynab-agent/interpret"
    finally:
        model._caller.reset(token)


def test_http_client_is_shared() -> None:
    """One process-wide client is reused (no per-call connection-pool leak)."""
    assert model._http_client() is model._http_client()


def test_build_model_wires_the_stamping_client() -> None:
    """build_model passes the shared, header-stamping client to the model."""
    assert model.build_model() is not None
    assert model._stamp_caller in model._http_client().event_hooks["request"]
