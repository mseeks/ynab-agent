"""Tests for the pure-core-isolation loop's deterministic AST import scan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.lib import APP_ROOT
from agents.pure_core_isolation import scan_pure_core_isolation

if TYPE_CHECKING:
    from pathlib import Path

_LAYER = "src/ynab_agent/domain"


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _scan(root: Path) -> set[str]:
    hits = scan_pure_core_isolation(root, root=root)
    return {h.kind for h in hits}


def test_spine_import_in_pure_layer_is_flagged(tmp_path: Path) -> None:
    _write(tmp_path, f"{_LAYER}/a.py", "from temporalio import workflow\n")
    assert _scan(tmp_path) == {"temporalio"}


def test_adapter_import_in_pure_layer_is_flagged(tmp_path: Path) -> None:
    _write(tmp_path, f"{_LAYER}/a.py", "from ynab_agent.ynab.client import X\n")
    assert _scan(tmp_path) == {"ynab_agent.ynab"}


def test_type_checking_import_is_also_flagged(tmp_path: Path) -> None:
    # The pure core should not even *type* against the spine.
    _write(
        tmp_path,
        f"{_LAYER}/a.py",
        "from typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n"
        "    from ynab_agent.workflow.runtime import WORKFLOWS\n",
    )
    assert _scan(tmp_path) == {"ynab_agent.workflow"}


def test_pydantic_and_pure_imports_are_clean(tmp_path: Path) -> None:
    _write(
        tmp_path,
        f"{_LAYER}/a.py",
        "from pydantic import Field\n"
        "from ynab_agent.domain.base import Frozen\n",
    )
    assert _scan(tmp_path) == set()


def test_spine_file_is_not_a_pure_layer(tmp_path: Path) -> None:
    # An import of the spine *from* the spine is fine — not a pure layer.
    _write(
        tmp_path,
        "src/ynab_agent/workflow/x.py",
        "from temporalio import workflow\n",
    )
    assert _scan(tmp_path) == set()


def test_real_pure_core_has_no_forbidden_imports() -> None:
    # The guard itself: the live pure core must stay isolated.
    assert scan_pure_core_isolation(APP_ROOT / "src") == []
