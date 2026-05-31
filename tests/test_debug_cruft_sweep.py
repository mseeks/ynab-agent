"""Tests for the debug-cruft loop's pure, deterministic signal sweep."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.debug_cruft import scan_debug_cruft

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_detects_each_marker_kind(tmp_path: Path) -> None:
    _write(
        tmp_path / "m.py",
        "\n".join(
            [
                "print('debugging')",
                "breakpoint()",
                "import pdb",
                "pdb.set_trace()",
                "@pytest.mark.skip(reason='flaky')",
                "pytest.xfail('known')",
            ]
        ),
    )
    kinds = sorted(h.kind for h in scan_debug_cruft(tmp_path))
    assert kinds == [
        "breakpoint",
        "import-pdb",
        "print",
        "pytest-skip",
        "pytest-skip-call",
        "set_trace",
    ]


def test_pprint_is_not_a_print(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "from pprint import pprint\npprint(data)\n")
    assert scan_debug_cruft(tmp_path) == []


def test_clean_file_yields_nothing(tmp_path: Path) -> None:
    _write(
        tmp_path / "clean.py",
        "def add(a: int, b: int) -> int:\n    return a + b\n",
    )
    assert scan_debug_cruft(tmp_path) == []


def test_agents_dir_is_excluded(tmp_path: Path) -> None:
    _write(tmp_path / "agents" / "loop.py", "print('harness output')")
    _write(tmp_path / "src" / "real.py", "print('leftover')")
    hits = scan_debug_cruft(tmp_path)
    assert len(hits) == 1
    assert hits[0].path.endswith("src/real.py")
