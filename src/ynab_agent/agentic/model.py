"""The model behind the agentic activities (SPEC §0.5).

A local Ollama running Gemma 4, driven through its OpenAI-compatible ``/v1`` by
Pydantic AI's OpenAI provider — the SPEC's chosen path (and its spike #2). Both
the model and the endpoint are env-overridable so a deployment can point at a
different Ollama host or model without code changes; tests pass their own
``TestModel``/``FunctionModel`` and never touch this.

This module lives outside the pure core and the workflow modules — the model
stack must never be imported into a Temporal workflow sandbox.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pydantic_ai import NativeOutput
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

if TYPE_CHECKING:
    from pydantic_ai import Agent
    from pydantic_ai.models import Model
    from pydantic_ai.settings import ModelSettings

# Gemma 4 12b: a practical balance of inference quality and latency across every
# task on a single local GPU. The larger 31b/26b dense weights gave deeper
# inference but made the balancer's reasoning generations too slow, so the app
# uses 12b. Needs Ollama >= 0.30. Env-overridable, so a dev box can point
# `YNAB_AGENT_MODEL` at another variant (`gemma4:e4b`, `gemma4:31b`).
_DEFAULT_MODEL = "gemma4:12b"
_DEFAULT_OLLAMA_URL = "http://localhost:11434/v1"

# Run Gemma 4 with reasoning ON: we want genuine inference on every
# task, not a reflex. The catch behind bug #15288: while "thinking", Gemma
# routes prose into the response's `reasoning` field and leaves `content`
# empty — fatal for *unconstrained* output. The production path forces NATIVE
# structured output (`NativeOutput`, a json_schema `response_format`), and under
# that constraint Gemma still emits schema-valid JSON into `content` while its
# chain-of-thought lands in `reasoning`. Verified on the Gemma 4 family / Ollama
# 0.24.0+: thinking + schema → valid content. We ask for "medium", not "high":
# on a single local GPU, high's long chain-of-thought made the heavier prompts
# (the balancer's whole-budget proposal) take many minutes; medium keeps real
# reasoning while cutting that tail. Two companion settings keep it robust:
# `max_tokens` is large so a chain-of-thought never starves the trailing JSON,
# and `timeout` is generous so a cold model load plus a reasoned generation is
# never cut off (the activity timeout in `constants` bounds the outer call). All
# sent via `extra_body`/settings on the `/v1` API.
_REASONED_GENERATION_TIMEOUT_S = 1200.0
_MAX_OUTPUT_TOKENS = 8192
_OLLAMA_SETTINGS: ModelSettings = {
    "extra_body": {"reasoning_effort": "medium"},
    "max_tokens": _MAX_OUTPUT_TOKENS,
    "timeout": _REASONED_GENERATION_TIMEOUT_S,
}


def build_model(
    *, model_name: str | None = None, base_url: str | None = None
) -> Model:
    """Build the configured Ollama/Gemma model (SPEC §0.5).

    Args:
        model_name: Override the model; defaults to ``$YNAB_AGENT_MODEL`` or
            ``gemma4:12b``.
        base_url: Override the endpoint; defaults to ``$YNAB_AGENT_OLLAMA_URL``
            or the local Ollama ``/v1``.

    Returns:
        A Pydantic AI model ready to pass to an agent run.
    """
    name = model_name or os.environ.get("YNAB_AGENT_MODEL", _DEFAULT_MODEL)
    url = base_url or os.environ.get(
        "YNAB_AGENT_OLLAMA_URL", _DEFAULT_OLLAMA_URL
    )
    # Ollama ignores the key but the OpenAI client requires a non-empty one.
    return OpenAIChatModel(
        name, provider=OpenAIProvider(base_url=url, api_key="ollama")
    )


async def run_structured[OutputT](
    agent: Agent[None, OutputT],
    prompt: str,
    *,
    output_type: type[OutputT],
    model: Model | None = None,
) -> OutputT:
    """Run an agentic agent and return its structured output (SPEC §0.5).

    The seam between the offline tests and production. Tests inject a
    ``TestModel`` and ride the agent's default *tool* output (which is all
    ``TestModel`` supports). Production passes ``model=None``: we build the real
    Ollama/Gemma and force **native** JSON-schema structured output.

    ``NativeOutput`` is what lets us keep Gemma 4's *reasoning on* (``_OLLAMA_
    SETTINGS``) without tripping bug #15288. While thinking, Gemma routes its
    prose into the response's ``reasoning`` field and leaves ``content`` empty —
    fatal for the default *tool* output (Pydantic AI then retries and echoes a
    ``content: null`` turn that Ollama rejects with ``400 invalid message
    content type: <nil>``). But ``NativeOutput`` asks Ollama to *constrain*
    generation to the JSON schema (`response_format: json_schema`), and under
    that constraint a schema-valid object lands in ``content`` on the first pass
    even as the chain-of-thought fills ``reasoning``. So production gets both:
    the model's deepest reasoning AND reliable structured output (verified on
    Gemma 4 / Ollama 0.24.0). The offline ``TestModel`` tests never touch
    this — they ride the default tool output, which is all ``TestModel``
    supports.

    Args:
        agent: The agent to run (its default output type is the tool-mode one
            the offline ``TestModel`` tests rely on).
        prompt: The rendered user prompt.
        output_type: The structured output type, wrapped in ``NativeOutput`` on
            the production path.
        model: A model to use; ``None`` selects the real Ollama/Gemma with
            native output. Tests pass a ``TestModel`` to stay offline.

    Returns:
        The agent's validated structured output.
    """
    if model is not None:
        result = await agent.run(prompt, model=model)
        return result.output
    result = await agent.run(
        prompt,
        model=build_model(),
        output_type=NativeOutput(output_type),
        model_settings=_OLLAMA_SETTINGS,
    )
    return result.output
