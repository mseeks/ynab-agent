"""Tests for the type-debt loop's pure, deterministic signal sweep."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.lib import SNIPPET_MAX
from agents.type_debt import scan_type_debt

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
                "x: Any = 1",
                "y = cast(int, x)",
                "z = bad()  # type: ignore[arg-type]",
                "# mypy: ignore-errors",
                "w = thing  # pyright: ignore",
            ]
        ),
    )
    kinds = sorted(h.kind for h in scan_type_debt(tmp_path))
    assert kinds == ["any", "cast", "mypy", "pyright", "type-ignore"]


def test_any_requires_typing_adjacency(tmp_path: Path) -> None:
    # `Any` in an import or inside a longer word is NOT a hit; only Any used in
    # an annotation position is.
    _write(
        tmp_path / "a.py",
        "\n".join(
            [
                "from typing import Any",
                "Anything = 1",
                "anybody = 2",
                "def f() -> Any: ...",  # this one IS a hit
            ]
        ),
    )
    hits = scan_type_debt(tmp_path)
    assert [h.line for h in hits] == [4]
    assert hits[0].kind == "any"


def test_any_in_generic_position(tmp_path: Path) -> None:
    _write(tmp_path / "g.py", "d: dict[str, Any] = {}")
    hits = scan_type_debt(tmp_path)
    assert len(hits) == 1
    assert hits[0].kind == "any"


def test_clean_file_yields_nothing(tmp_path: Path) -> None:
    _write(
        tmp_path / "clean.py",
        "def add(a: int, b: int) -> int:\n    return a + b\n",
    )
    assert scan_type_debt(tmp_path) == []


def test_agents_dir_is_excluded(tmp_path: Path) -> None:
    # The loops contain these patterns as string literals; never self-flag them.
    _write(tmp_path / "agents" / "loop.py", "x: Any = 1")
    _write(tmp_path / "src" / "real.py", "y: Any = 2")
    hits = scan_type_debt(tmp_path)
    assert len(hits) == 1
    assert hits[0].path.endswith("src/real.py")
    assert not any("agents" in h.path.split("/") for h in hits)


def test_scanning_a_single_file(tmp_path: Path) -> None:
    target = _write(tmp_path / "one.py", "v = cast(str, 1)")
    hits = scan_type_debt(target)
    assert len(hits) == 1
    assert hits[0].kind == "cast"


def test_non_python_files_ignored(tmp_path: Path) -> None:
    _write(tmp_path / "notes.md", "x: Any = 1")
    assert scan_type_debt(tmp_path) == []


def test_snippet_is_trimmed(tmp_path: Path) -> None:
    long_line = "x: Any = " + "1" * 500
    _write(tmp_path / "long.py", long_line)
    hits = scan_type_debt(tmp_path)
    assert len(hits) == 1
    assert len(hits[0].text) == SNIPPET_MAX


def test_line_numbers_are_one_based(tmp_path: Path) -> None:
    _write(tmp_path / "n.py", "ok = 1\n\nbad: Any = 2\n")
    hits = scan_type_debt(tmp_path)
    assert [h.line for h in hits] == [3]
