"""Tests for the shared agentic model helper (SPEC §0.5).

These cover the seam :func:`run_structured` draws between the offline tests and
production: an injected ``TestModel`` rides the agent's default tool output,
while the production path (``model is None``) forces **native** JSON-schema
structured output — the SPEC §0.5 mitigation for Ollama bug #15288 (Gemma 4 text
routed into ``reasoning``, leaving ``content`` empty and a tool-call retry
sending the null content Ollama rejects). The selection is asserted offline by
capturing what reaches ``Agent.run``, so the production output mode is covered
without a network call.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel

from ynab_agent.agentic.model import _OLLAMA_SETTINGS, run_structured

if TYPE_CHECKING:
    import pytest


class _Out(BaseModel):
    value: str


def _capture(calls: list[dict[str, Any]]) -> Any:
    async def fake_run(
        self: Agent[Any, Any], prompt: str, **kwargs: Any
    ) -> SimpleNamespace:
        calls.append({"prompt": prompt, **kwargs})
        return SimpleNamespace(output=_Out(value="ok"))

    return fake_run


async def test_injected_model_rides_the_default_tool_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tests pass a TestModel; the helper must not override the output mode (it
    # uses the agent's default tool output, all TestModel supports).
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(Agent, "run", _capture(calls))
    agent: Agent[None, _Out] = Agent(output_type=_Out, system_prompt="x")
    test_model = TestModel(custom_output_args={"value": "ok"})

    out = await run_structured(agent, "p", output_type=_Out, model=test_model)

    assert out.value == "ok"
    assert calls[0]["model"] is test_model
    assert "output_type" not in calls[0]


async def test_production_path_forces_native_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # model is None → build the real Ollama model AND force native output.
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(Agent, "run", _capture(calls))
    agent: Agent[None, _Out] = Agent(output_type=_Out, system_prompt="x")

    out = await run_structured(agent, "p", output_type=_Out, model=None)

    assert out.value == "ok"
    assert isinstance(calls[0]["model"], OpenAIChatModel)
    assert isinstance(calls[0]["output_type"], NativeOutput)
    # The reasoning-suppression setting is the heart of the #15288 fix — assert
    # it reaches the run, so a refactor can't silently drop it.
    assert calls[0]["model_settings"] == _OLLAMA_SETTINGS
    assert (
        calls[0]["model_settings"]["extra_body"]["reasoning_effort"] == "none"
    )
