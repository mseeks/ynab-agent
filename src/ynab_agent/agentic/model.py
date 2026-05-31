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

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

if TYPE_CHECKING:
    from pydantic_ai.models import Model

# Gemma 4's efficient variant: small and fast enough for dev, env-overridable.
_DEFAULT_MODEL = "gemma4:e4b"
_DEFAULT_OLLAMA_URL = "http://localhost:11434/v1"


def build_model(
    *, model_name: str | None = None, base_url: str | None = None
) -> Model:
    """Build the configured Ollama/Gemma model (SPEC §0.5).

    Args:
        model_name: Override the model; defaults to ``$YNAB_AGENT_MODEL`` or
            ``gemma4:e4b``.
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
