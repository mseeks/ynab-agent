"""The activity retry policy's deterministic-failure denylist (SPEC §13)."""

from __future__ import annotations

from ynab_agent.workflow.constants import ACTIVITY_RETRY


def test_model_glitches_retry_but_real_bugs_fail_fast() -> None:
    deny = set(ACTIVITY_RETRY.non_retryable_error_types or ())
    # A transient model-output glitch (a leaked chat-template token, malformed
    # JSON) must be RETRIED — a fresh generation almost always parses. Debugged
    # from a live balance-offer failure on the Transportation category, where a
    # flaky reply-read raised this and (being terminal) killed the whole offer.
    assert "UnexpectedModelBehavior" not in deny
    # Genuine bugs still fail fast rather than spin (bounded by attempts).
    assert {"ValueError", "AttributeError", "ValidationError"} <= deny


def test_retry_is_bounded_by_attempts() -> None:
    # The backstop that lets us retry model glitches safely: even a repeatably
    # stuck model gives up after a bounded number of attempts, never spins.
    assert ACTIVITY_RETRY.maximum_attempts == 10
