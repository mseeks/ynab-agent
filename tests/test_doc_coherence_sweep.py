"""Tests for the doc-coherence loop's pure, deterministic drift scan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.doc_coherence import scan_doc_drift

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _kinds(path: Path) -> set[str]:
    return {h.kind for h in scan_doc_drift(path)}


def test_broken_relative_link_detected(tmp_path: Path) -> None:
    _write(tmp_path / "d.md", "See [the spec](./missing.md) for details.")
    assert "broken-link" in _kinds(tmp_path)


def test_valid_link_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path / "present.md", "content")
    _write(tmp_path / "d.md", "See [here](./present.md).")
    hits = [h for h in scan_doc_drift(tmp_path) if h.path.endswith("d.md")]
    assert hits == []


def test_http_links_skipped(tmp_path: Path) -> None:
    _write(tmp_path / "d.md", "[site](https://example.com/page)")
    assert scan_doc_drift(tmp_path) == []


def test_missing_backtick_path_detected(tmp_path: Path) -> None:
    _write(tmp_path / "d.md", "Edit `src/ynab_agent/nope.py` to fix it.")
    assert "missing-path" in _kinds(tmp_path)


def test_existing_path_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path / "d.md", "See `pyproject.toml` for config.")
    assert "missing-path" not in _kinds(tmp_path)


def test_code_identifier_is_not_treated_as_a_path(tmp_path: Path) -> None:
    _write(tmp_path / "d.md", "The `advance` function and the `Money` type.")
    assert "missing-path" not in _kinds(tmp_path)


def test_missing_make_target_detected(tmp_path: Path) -> None:
    _write(tmp_path / "d.md", "Run `make bogus-xyz` to build.")
    assert "missing-make" in _kinds(tmp_path)


def test_existing_make_target_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path / "d.md", "Run `make sync` first.")
    assert "missing-make" not in _kinds(tmp_path)
