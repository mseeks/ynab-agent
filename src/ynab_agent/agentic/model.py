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

# Gemma 4 at its largest weight: we trade latency for the deepest inference on
# every task — categorization should feel like a genuinely intelligent agent,
# and the household's volume never needs throughput. Env-overridable, so a dev
# box can point `YNAB_AGENT_MODEL` at a smaller variant (`gemma4:e4b`).
_DEFAULT_MODEL = "gemma4:31b"
_DEFAULT_OLLAMA_URL = "http://localhost:11434/v1"

# Turn Gemma's "thinking" OFF for the production runs. Ollama bug #15288
# otherwise routes Gemma 4's answer into the response's `reasoning` field and
# leaves `content` empty/unparsable, which is what breaks structured output (see
# `run_structured`). `reasoning_effort: "none"` suppresses it so the answer
# lands in `content` as schema-valid JSON; it is sent raw via `extra_body`
# because Ollama honours it on the OpenAI-compatible `/v1` (a top-level
# `think: false` is silently ignored there).
_OLLAMA_SETTINGS: ModelSettings = {"extra_body": {"reasoning_effort": "none"}}


def build_model(
    *, model_name: str | None = None, base_url: str | None = None
) -> Model:
    """Build the configured Ollama/Gemma model (SPEC §0.5).

    Args:
        model_name: Override the model; defaults to ``$YNAB_AGENT_MODEL`` or
            ``gemma4:31b`` (the largest Gemma 4 weight).
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

    The production path is the SPEC §0.5 mitigation for Ollama bug #15288, which
    routes Gemma 4's text into the response's ``reasoning`` field and leaves
    ``content`` empty. With the default tool output that empty content is fatal:
    when Gemma fails to emit the output tool call (it intermittently "thinks"
    instead), Pydantic AI retries and echoes the prior turn back as an assistant
    message with ``content: null`` and no ``tool_calls`` — which Ollama rejects
    with ``400 invalid message content type: <nil>``, failing the whole activity
    (no categorization, no reply). Two levers close that off together:

    - ``reasoning_effort: "none"`` (``_OLLAMA_SETTINGS``) turns the thinking
      off at the source, so the answer lands in ``content``, not ``reasoning``.
    - ``NativeOutput`` asks Ollama to constrain generation to the schema, so a
      schema-valid object lands in ``content`` on the first pass — no
      output-tool gamble, no null-content retry.

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
