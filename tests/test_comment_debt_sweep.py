"""Tests for the comment-debt loop's pure, deterministic signal sweep."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.comment_debt import scan_comment_debt

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
                "x = 1  # TODO: wire this up",
                "y = 2  # FIXME later",
                "z = 3  # HACK around the API",
                "w = 4  # XXX revisit",
            ]
        ),
    )
    kinds = sorted(h.kind for h in scan_comment_debt(tmp_path))
    assert kinds == ["FIXME", "HACK", "TODO", "XXX"]


def test_markers_are_case_sensitive_whole_words(tmp_path: Path) -> None:
    _write(
        tmp_path / "a.py",
        "\n".join(
            [
                "todo = 1  # lowercase is not a marker",
                "HACKATHON = 2  # substring is not a marker",
                "fixme_later = 3",
            ]
        ),
    )
    assert scan_comment_debt(tmp_path) == []


def test_clean_file_yields_nothing(tmp_path: Path) -> None:
    _write(
        tmp_path / "clean.py",
        "def add(a: int, b: int) -> int:\n    return a + b\n",
    )
    assert scan_comment_debt(tmp_path) == []


def test_agents_dir_is_excluded(tmp_path: Path) -> None:
    _write(tmp_path / "agents" / "loop.py", "x = 1  # TODO self")
    _write(tmp_path / "src" / "real.py", "y = 2  # TODO real")
    hits = scan_comment_debt(tmp_path)
    assert len(hits) == 1
    assert hits[0].path.endswith("src/real.py")


def test_line_numbers_are_one_based(tmp_path: Path) -> None:
    _write(tmp_path / "n.py", "ok = 1\n\nbad = 2  # FIXME\n")
    hits = scan_comment_debt(tmp_path)
    assert [h.line for h in hits] == [3]
