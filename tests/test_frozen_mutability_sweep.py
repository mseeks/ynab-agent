"""Tests for the frozen-mutability loop's pure AST scan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.frozen_mutability import scan_frozen_mutability

if TYPE_CHECKING:
    from pathlib import Path

_FROZEN_BASE = (
    "class Frozen(BaseModel):\n    model_config = ConfigDict(frozen=True)\n\n\n"
)


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_dict_field_on_frozen_subclass_is_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "m.py",
        _FROZEN_BASE + "class P(Frozen):\n"
        "    d: dict[str, int] = {}\n"
        "    t: tuple[int, ...] = ()\n",
    )
    hits = scan_frozen_mutability(tmp_path)
    assert {h.kind for h in hits} == {"dict"}
    assert all("d:" in h.text for h in hits)


def test_list_and_set_fields_are_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "m.py",
        _FROZEN_BASE + "class P(Frozen):\n"
        "    xs: list[int] = []\n"
        "    ys: set[int] = set()\n",
    )
    assert {h.kind for h in scan_frozen_mutability(tmp_path)} == {
        "list",
        "set",
    }


def test_immutable_fields_are_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "m.py",
        _FROZEN_BASE + "class P(Frozen):\n"
        "    t: tuple[int, ...] = ()\n"
        "    m: Mapping[str, int] = {}\n"
        "    fs: frozenset[int] = frozenset()\n",
    )
    assert scan_frozen_mutability(tmp_path) == []


def test_non_frozen_class_is_not_scanned(tmp_path: Path) -> None:
    _write(
        tmp_path / "m.py",
        "class Plain(BaseModel):\n    d: dict[str, int] = {}\n",
    )
    assert scan_frozen_mutability(tmp_path) == []


def test_optional_dict_is_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "m.py",
        "class P(Frozen):\n    d: dict[str, int] | None = None\n",
    )
    assert {h.kind for h in scan_frozen_mutability(tmp_path)} == {"dict"}


def test_transitive_frozen_subclass_is_flagged(tmp_path: Path) -> None:
    # Frozen-ness closes over subclass-of-a-frozen-subclass, across files.
    _write(tmp_path / "a.py", _FROZEN_BASE)
    _write(
        tmp_path / "b.py",
        "class Mid(Frozen):\n    pass\n\n\nclass Leaf(Mid):\n"
        "    s: set[int] = set()\n",
    )
    assert {h.kind for h in scan_frozen_mutability(tmp_path)} == {"set"}


def test_frozen_via_model_config_without_base_is_flagged(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "m.py",
        "class Standalone(BaseModel):\n"
        "    model_config = ConfigDict(frozen=True)\n"
        "    items: list[int] = []\n",
    )
    assert {h.kind for h in scan_frozen_mutability(tmp_path)} == {"list"}
