"""Tests for the model-seam loop's pure sweep."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.model_seam import scan_model_seam

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_direct_agent_run_in_agentic_is_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "ynab_agent/agentic/enrich.py",
        "result = await _AGENT.run(prompt, model=model)\n",
    )
    assert {h.kind for h in scan_model_seam(tmp_path)} == {"direct-run"}


def test_run_structured_call_is_not_flagged(tmp_path: Path) -> None:
    # The sanctioned path: no bare `.run(`, so nothing fires.
    _write(
        tmp_path / "ynab_agent/agentic/enrich.py",
        "return await run_structured(_AGENT, prompt, output_type=X)\n",
    )
    assert scan_model_seam(tmp_path) == []


def test_agent_run_inside_the_seam_module_is_not_flagged(
    tmp_path: Path,
) -> None:
    # `agent.run` is sanctioned inside agentic/model.py (it IS run_structured).
    _write(
        tmp_path / "ynab_agent/agentic/model.py",
        "    result = await agent.run(prompt, model=model)\n",
    )
    assert scan_model_seam(tmp_path) == []


def test_agent_construction_outside_agentic_is_flagged(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "ynab_agent/workflow/wf.py",
        "rogue = Agent(output_type=X, system_prompt='y')\n",
    )
    assert {h.kind for h in scan_model_seam(tmp_path)} == {"external-agent"}


def test_agent_construction_inside_agentic_is_not_flagged(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "ynab_agent/agentic/classify.py",
        "_AGENT: Agent[None, X] = Agent(output_type=X, system_prompt='y')\n",
    )
    assert scan_model_seam(tmp_path) == []


def test_agent_type_annotation_outside_agentic_is_not_flagged(
    tmp_path: Path,
) -> None:
    # `Agent[...]` is a subscript (type), not an `Agent(` construction.
    _write(
        tmp_path / "ynab_agent/workflow/types.py",
        "def f(a: Agent[None, X]) -> None: ...\n",
    )
    assert scan_model_seam(tmp_path) == []


def test_private_agent_import_is_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "ynab_agent/workflow/wf.py",
        "from ynab_agent.agentic.enrich import _AGENT\n",
    )
    assert {h.kind for h in scan_model_seam(tmp_path)} == {"agent-import"}
