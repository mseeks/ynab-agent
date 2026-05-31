"""Tests for the sandbox-imports loop's pure AST scan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.sandbox_imports import scan_sandbox_imports

if TYPE_CHECKING:
    from pathlib import Path

_WF = "@workflow.defn\nclass W:\n    @workflow.run\n    async def run(self):\n"


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_module_level_forbidden_import_in_workflow_is_flagged(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "wf.py",
        "from pydantic_ai import Agent\n\n\n" + _WF + "        pass\n",
    )
    hits = scan_sandbox_imports(tmp_path)
    assert {h.kind for h in hits} == {"pydantic_ai"}


def test_lazy_import_inside_a_function_is_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "act.py",
        "from temporalio import activity\n\n\n"
        "@activity.defn\nasync def enrich():\n"
        "    from ynab_agent.agentic.enrich import propose\n"
        "    return propose\n",
    )
    assert scan_sandbox_imports(tmp_path) == []


def test_type_checking_import_is_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "act.py",
        "from typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n    from agentmail import AgentMail\n\n\n"
        "@activity.defn\nasync def send():\n    pass\n",
    )
    assert scan_sandbox_imports(tmp_path) == []


def test_passthrough_block_forbidden_import_is_flagged(tmp_path: Path) -> None:
    # The imports_passed_through() block is exactly what the sandbox re-runs.
    _write(
        tmp_path / "wf.py",
        "from temporalio import workflow\n\n"
        "with workflow.unsafe.imports_passed_through():\n"
        "    from ynab_agent.mail.client import MailClient\n\n\n"
        + _WF
        + "        pass\n",
    )
    assert {h.kind for h in scan_sandbox_imports(tmp_path)} == {
        "ynab_agent.mail"
    }


def test_module_level_httpx_in_an_activity_is_flagged(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "act.py",
        "import httpx\n\n\n@activity.defn\nasync def fetch():\n    pass\n",
    )
    assert {h.kind for h in scan_sandbox_imports(tmp_path)} == {"httpx"}


def test_non_workflow_file_is_ignored(tmp_path: Path) -> None:
    # No @workflow.defn / @activity.defn → not part of the sandbox graph.
    _write(
        tmp_path / "plain.py", "from pydantic_ai import Agent\n\nx = Agent\n"
    )
    assert scan_sandbox_imports(tmp_path) == []


def test_domain_imports_in_a_workflow_are_not_forbidden(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "wf.py",
        "from ynab_agent.domain.money import Money\n\n\n"
        + _WF
        + "        pass\n",
    )
    assert scan_sandbox_imports(tmp_path) == []
