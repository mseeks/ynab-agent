"""Tests for the duplicated-constant loop's pure, deterministic scan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.duplicated_constant import scan_duplicated_constants

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_value_at_two_rule_sites_is_clustered(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "if amount > 75:\n    drop()\n")
    _write(tmp_path / "b.py", "window = timedelta(hours=75)\n")
    hits = scan_duplicated_constants(tmp_path)
    assert {h.kind for h in hits} == {"75"}
    assert len(hits) == 2


def test_single_site_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "if amount > 75:\n    drop()\n")
    assert scan_duplicated_constants(tmp_path) == []


def test_trivial_values_excluded(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "if x > 0:\n    pass\n")
    _write(tmp_path / "b.py", "if y >= 1:\n    pass\n")
    assert scan_duplicated_constants(tmp_path) == []


def test_non_rule_lines_ignored(tmp_path: Path) -> None:
    # Plain assignments are not rule-bearing, even with a repeated value.
    _write(tmp_path / "a.py", "x = 75\n")
    _write(tmp_path / "b.py", "y = 75\n")
    assert scan_duplicated_constants(tmp_path) == []


def test_agents_dir_is_excluded(tmp_path: Path) -> None:
    _write(tmp_path / "agents" / "loop.py", "if x > 99:\n    pass\n")
    _write(tmp_path / "src" / "real.py", "if y > 99:\n    pass\n")
    # Only the src site is scanned → one distinct site → not a cluster.
    assert scan_duplicated_constants(tmp_path) == []
