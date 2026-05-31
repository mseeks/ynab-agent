"""Tests for the secret-leak loop's pure scan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents.secret_leak import scan_secret_leaks

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_hardcoded_key_is_flagged(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", 'api_key = "sk-live-abcdef0123456789xyz"\n')
    assert len(scan_secret_leaks(tmp_path)) == 1


def test_reading_from_env_is_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "a.py",
        'api_key = os.environ["AGENTMAIL_API_KEY"]\n',
    )
    assert scan_secret_leaks(tmp_path) == []


def test_short_dummy_value_is_not_flagged(tmp_path: Path) -> None:
    # The Ollama dummy key ("ollama") is under the 16-char threshold.
    _write(tmp_path / "a.py", 'api_key = "ollama"\n')
    assert scan_secret_leaks(tmp_path) == []


def test_placeholder_value_is_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", 'token = "your-token-goes-here-please"\n')
    assert scan_secret_leaks(tmp_path) == []


def test_non_secret_name_is_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", 'subject = "a fairly long email subject line"\n')
    assert scan_secret_leaks(tmp_path) == []


def test_password_literal_is_flagged(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", 'password = "hunter2hunter2hunter2"\n')
    assert len(scan_secret_leaks(tmp_path)) == 1
