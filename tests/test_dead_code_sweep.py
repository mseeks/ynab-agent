"""Tests for the dead-code loop's pure, deterministic AST + reference scan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.dead_code import scan_dead_code

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _scan(root: Path) -> set[str]:
    hits = scan_dead_code(root, reference_roots=(root,))
    return {h.kind for h in hits}


def test_unreferenced_function_is_flagged(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "def orphan() -> int:\n    return 1\n")
    assert _scan(tmp_path) == {"orphan"}


def test_referenced_function_is_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "def used() -> int:\n    return 1\n")
    _write(tmp_path / "b.py", "from a import used\n\nprint(used())\n")
    assert _scan(tmp_path) == set()


def test_dunder_assignment_export_is_excluded(tmp_path: Path) -> None:
    _write(
        tmp_path / "a.py",
        '__all__ = ["PublicApi"]\n\n\nclass PublicApi:\n    pass\n',
    )
    assert _scan(tmp_path) == set()


def test_only_top_level_defs_are_candidates(tmp_path: Path) -> None:
    # A nested function is not a module-level symbol → never a candidate.
    _write(
        tmp_path / "a.py",
        "def outer() -> int:\n"
        "    def inner() -> int:\n"
        "        return 2\n"
        "    return inner()\n",
    )
    # `outer` itself is unreferenced → flagged; `inner` is not top-level.
    assert _scan(tmp_path) == {"outer"}


def test_class_referenced_only_as_a_field_type_is_not_flagged(
    tmp_path: Path,
) -> None:
    # The word-boundary count makes a type annotation a real reference.
    _write(tmp_path / "a.py", "class LineItem:\n    pass\n")
    _write(
        tmp_path / "b.py",
        "from a import LineItem\n\n\n"
        "class Receipt:\n    items: list[LineItem]\n",
    )
    assert "LineItem" not in _scan(tmp_path)


def test_agents_dir_is_excluded(tmp_path: Path) -> None:
    # Definitions under agents/ are never scanned (loop self-exclusion).
    _write(
        tmp_path / "agents" / "loop.py", "def orphan() -> int:\n    return 1\n"
    )
    assert _scan(tmp_path) == set()
