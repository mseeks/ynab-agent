"""Tests for the determinism loop's pure workflow-hazard scan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.determinism import scan_determinism_hazards

if TYPE_CHECKING:
    from pathlib import Path

_WF = "@workflow.defn\nclass W:\n    @workflow.run\n    async def run(self):\n"


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_bare_datetime_now_in_workflow_is_flagged(tmp_path: Path) -> None:
    _write(tmp_path / "wf.py", _WF + "        t = datetime.now()\n")
    hits = scan_determinism_hazards(tmp_path)
    assert {h.kind for h in hits} == {"datetime.now"}


def test_workflow_now_is_not_flagged(tmp_path: Path) -> None:
    # The safe replacement has no `datetime.` prefix → cannot match.
    _write(tmp_path / "wf.py", _WF + "        t = workflow.now()\n")
    assert scan_determinism_hazards(tmp_path) == []


def test_nondeterminism_in_a_non_workflow_file_is_ignored(
    tmp_path: Path,
) -> None:
    # No @workflow.defn → an activity module → anything goes.
    _write(
        tmp_path / "activities.py",
        "import datetime\n\n\nasync def act():\n    return datetime.now()\n",
    )
    assert scan_determinism_hazards(tmp_path) == []


def test_random_and_uuid_modules_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "wf.py",
        _WF + "        x = random.random()\n        y = uuid.uuid4()\n",
    )
    assert {h.kind for h in scan_determinism_hazards(tmp_path)} == {
        "random module",
        "uuid module",
    }


def test_workflow_random_and_uuid_helpers_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "wf.py",
        _WF + "        x = workflow.random()\n        y = workflow.uuid4()\n",
    )
    assert scan_determinism_hazards(tmp_path) == []


def test_asyncio_sleep_flagged_workflow_sleep_not(tmp_path: Path) -> None:
    _write(
        tmp_path / "wf.py",
        _WF + "        await asyncio.sleep(1)\n"
        "        await workflow.sleep(1)\n",
    )
    assert {h.kind for h in scan_determinism_hazards(tmp_path)} == {
        "asyncio.sleep"
    }


def test_datetime_type_annotation_still_flags_only_calls(
    tmp_path: Path,
) -> None:
    # `datetime.datetime` as a type is not a `.now()` call → no hit.
    _write(
        tmp_path / "wf.py",
        _WF + "        self._x: dict[str, datetime.datetime] = {}\n",
    )
    assert scan_determinism_hazards(tmp_path) == []
