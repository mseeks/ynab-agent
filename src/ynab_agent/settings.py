"""Deployment settings, read from the environment or a ``.env`` file.

The non-secret config a running worker needs: which AgentMail inbox the agent
sends from, and who the household recipients are. The budget id and the API keys
are each the concern of the client that uses them (so the YNAB client never
needs AgentMail config to construct, and vice versa), and the secrets are read
straight from the environment by those clients — none of it lives in the repo.

Built on ``pydantic-settings`` for declarative, validated env binding (prefix
``YNAB_AGENT_``) with ``.env`` support, frozen like the rest of the domain.
``owners`` is ``NoDecode`` so the env var is a comma-separated address list
rather than JSON.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Non-secret worker config from ``YNAB_AGENT_*`` (env or ``.env``)."""

    model_config = SettingsConfigDict(
        env_prefix="YNAB_AGENT_",
        env_file=".env",
        extra="ignore",
        frozen=True,
    )

    inbox: str = Field(min_length=1)
    owners: Annotated[tuple[str, ...], NoDecode] = Field(min_length=1)

    @field_validator("owners", mode="before")
    @classmethod
    def _split_owners(cls, value: object) -> object:
        """Accept ``YNAB_AGENT_OWNERS`` as a comma-separated address list."""
        if isinstance(value, str):
            return tuple(
                address.strip()
                for address in value.split(",")
                if address.strip()
            )
        return value
