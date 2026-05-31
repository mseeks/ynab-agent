"""Tests for the test-backfill loop's pure reference scan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.test_backfill import scan_test_backfill

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _scan(src: Path, tests: Path) -> set[str]:
    hits = scan_test_backfill(src, src_root=src, tests_root=tests)
    return {h.kind for h in hits}


def test_public_symbol_used_in_src_but_untested_is_flagged(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    _write(src / "a.py", "def gate() -> int:\n    return 1\n")
    _write(src / "b.py", "from a import gate\n\nprint(gate())\n")
    _write(tests / "test_other.py", "def test_unrelated() -> None:\n    pass\n")
    assert _scan(src, tests) == {"gate"}


def test_symbol_named_in_a_test_is_not_flagged(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    _write(src / "a.py", "def gate() -> int:\n    return 1\n")
    _write(src / "b.py", "from a import gate\n\nprint(gate())\n")
    _write(tests / "test_gate.py", "from a import gate\n\nassert gate() == 1\n")
    assert _scan(src, tests) == set()


def test_dead_symbol_is_not_a_backfill_candidate(tmp_path: Path) -> None:
    # Used nowhere in src (count == 1): that is the dead-code loop's job.
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    _write(src / "a.py", "def orphan() -> int:\n    return 1\n")
    assert _scan(src, tests) == set()


def test_private_symbols_are_not_candidates(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    _write(src / "a.py", "def _helper() -> int:\n    return 1\n")
    _write(src / "b.py", "from a import _helper\n\nprint(_helper())\n")
    assert _scan(src, tests) == set()


def test_public_class_used_but_untested_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    _write(src / "a.py", "class Policy:\n    pass\n")
    _write(src / "b.py", "from a import Policy\n\np = Policy()\n")
    assert _scan(src, tests) == {"Policy"}
