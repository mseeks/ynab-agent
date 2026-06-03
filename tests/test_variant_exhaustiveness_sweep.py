"""Tests for the variant-exhaustiveness loop's deterministic AST scan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.variant_exhaustiveness import scan_variant_exhaustiveness

if TYPE_CHECKING:
    from pathlib import Path

_UNION = (
    "from typing import Annotated\n"
    "from pydantic import Field\n"
    "class A: ...\n"
    "class B: ...\n"
    'U = Annotated[A | B, Field(discriminator="kind")]\n'
)


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _scan(root: Path) -> set[str]:
    hits = scan_variant_exhaustiveness(root, reference_roots=(root,))
    return {h.kind for h in hits}


def test_members_with_no_dispatch_are_flagged(tmp_path: Path) -> None:
    _write(tmp_path / "u.py", _UNION)
    assert _scan(tmp_path) == {"A", "B"}


def test_case_pattern_counts_as_dispatch(tmp_path: Path) -> None:
    _write(tmp_path / "u.py", _UNION)
    _write(
        tmp_path / "fold.py",
        "def f(x: object) -> int:\n"
        "    match x:\n"
        "        case A():\n"
        "            return 1\n"
        "        case _:\n"
        "            return 0\n",
    )
    # A is matched; B still has no branch.
    assert _scan(tmp_path) == {"B"}


def test_isinstance_counts_as_dispatch(tmp_path: Path) -> None:
    _write(tmp_path / "u.py", _UNION)
    _write(
        tmp_path / "fold.py",
        "def f(x: object) -> int:\n"
        "    return 1 if isinstance(x, (A, B)) else 0\n",
    )
    assert _scan(tmp_path) == set()


def test_non_discriminated_union_is_ignored(tmp_path: Path) -> None:
    # Annotated without a Field(discriminator=...) is not a tagged union.
    _write(
        tmp_path / "u.py",
        "from typing import Annotated\n"
        "class A: ...\n"
        "class B: ...\n"
        'W = Annotated[A | B, "doc"]\n',
    )
    assert _scan(tmp_path) == set()


def test_real_src_scan_runs_without_error() -> None:
    # The loop runs clean against the live tree; findings (if any) are for the
    # agent to sort, so we only assert the scan executes and is well-formed.
    from agents.lib import APP_ROOT

    hits = scan_variant_exhaustiveness(APP_ROOT / "src")
    assert all(h.kind and h.path for h in hits)
