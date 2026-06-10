"""Tests for the deployment settings (pydantic-settings)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ynab_agent.settings import Settings


def test_reads_inbox_and_splits_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YNAB_AGENT_INBOX", "agent@agentmail.to")
    monkeypatch.setenv("YNAB_AGENT_OWNERS", "a@x.com, b@x.com")
    settings = Settings()
    assert settings.inbox == "agent@agentmail.to"
    assert settings.owners == ("a@x.com", "b@x.com")


def test_missing_inbox_is_a_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("YNAB_AGENT_INBOX", raising=False)
    monkeypatch.setenv("YNAB_AGENT_OWNERS", "a@x.com")
    with pytest.raises(ValidationError):
        Settings()


def test_empty_owners_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YNAB_AGENT_INBOX", "agent@agentmail.to")
    monkeypatch.setenv("YNAB_AGENT_OWNERS", "")
    with pytest.raises(ValidationError):
        Settings()


def test_init_kwargs_override_the_environment() -> None:
    settings = Settings(inbox="agent@agentmail.to", owners=("only@x.com",))
    assert settings.owners == ("only@x.com",)


def test_allowed_senders_split_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An owner's secondary address may ACT (allow-listed) without being
    # mailed — it parses like owners but defaults to empty.
    monkeypatch.setenv("YNAB_AGENT_INBOX", "inbox-1")
    monkeypatch.setenv("YNAB_AGENT_OWNERS", "a@x.com")
    monkeypatch.delenv("YNAB_AGENT_ALLOWED_SENDERS", raising=False)
    assert Settings().allowed_senders == ()
    monkeypatch.setenv(
        "YNAB_AGENT_ALLOWED_SENDERS", "me@icloud.com, alias@x.com"
    )
    assert Settings().allowed_senders == ("me@icloud.com", "alias@x.com")
