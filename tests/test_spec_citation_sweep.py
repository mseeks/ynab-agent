"""Tests for the spec-citation loop's deterministic citation scan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.lib import APP_ROOT
from agents.spec_citation import scan_spec_citations, valid_sections

if TYPE_CHECKING:
    from pathlib import Path

_SPEC = (
    "# Spec\n"
    "## 0. Foundations\n"
    "The boundary §0.5 is defined inline here.\n"
    "## 4. Policies\n"
    "### 4.2 Autonomy\n"
    "## 14. Learning\n"
)


def _spec(tmp_path: Path) -> Path:
    path = tmp_path / "SPEC.md"
    path.write_text(_SPEC)
    return path


def test_valid_sections_includes_headings_and_inline_refs(
    tmp_path: Path,
) -> None:
    ids = valid_sections(_spec(tmp_path))
    assert {"0", "4", "4.2", "14", "0.5"} <= ids


def test_dangling_citation_is_flagged(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    src = tmp_path / "m.py"
    src.write_text('"""Does a thing (SPEC §99)."""\n')
    hits = scan_spec_citations(src, spec_path=spec)
    assert [h.kind for h in hits] == ["§99"]


def test_valid_citations_are_not_flagged(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    src = tmp_path / "m.py"
    src.write_text('"""Per SPEC §4.2 and §0.5 and §14."""\n')
    assert scan_spec_citations(src, spec_path=spec) == []


def test_real_src_has_no_dangling_spec_citations() -> None:
    # The guard: every SPEC §ref in the live code resolves to a real section.
    dangling = scan_spec_citations(APP_ROOT / "src")
    assert dangling == [], [f"{h.path}:{h.line} {h.kind}" for h in dangling]
