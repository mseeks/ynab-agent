"""Tests for the activity-retry loop's pure AST scan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.activity_retry import scan_activity_retry

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_call_without_retry_policy_is_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "wf.py",
        "async def f():\n    await workflow.execute_activity(act, args)\n",
    )
    assert {h.kind for h in scan_activity_retry(tmp_path)} == {
        "execute_activity"
    }


def test_call_with_retry_policy_is_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "wf.py",
        "async def f():\n"
        "    await workflow.execute_activity(act, retry_policy=RP())\n",
    )
    assert scan_activity_retry(tmp_path) == []


def test_retry_policy_on_a_later_line_is_not_flagged(tmp_path: Path) -> None:
    # The AST walk sees the whole call; a line regex would miss this.
    _write(
        tmp_path / "wf.py",
        "async def f():\n"
        "    await workflow.execute_activity(\n"
        "        act,\n"
        "        args=[x],\n"
        "        start_to_close_timeout=T,\n"
        "        retry_policy=RetryPolicy(maximum_attempts=3),\n"
        "    )\n",
    )
    assert scan_activity_retry(tmp_path) == []


def test_execute_local_activity_without_policy_is_flagged(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "wf.py",
        "async def f():\n"
        "    await workflow.execute_local_activity(act, "
        "start_to_close_timeout=T)\n",
    )
    assert {h.kind for h in scan_activity_retry(tmp_path)} == {
        "execute_local_activity"
    }


def test_kwargs_spread_is_treated_as_policy_present(tmp_path: Path) -> None:
    # A `**opts` spread could carry retry_policy; don't false-flag it.
    _write(
        tmp_path / "wf.py",
        "async def f():\n    await workflow.execute_activity(act, **opts)\n",
    )
    assert scan_activity_retry(tmp_path) == []


def test_unrelated_calls_are_ignored(tmp_path: Path) -> None:
    _write(
        tmp_path / "wf.py",
        "async def f():\n"
        "    await workflow.execute_child_workflow(W.run, x)\n"
        "    helper.run(y)\n",
    )
    assert scan_activity_retry(tmp_path) == []
