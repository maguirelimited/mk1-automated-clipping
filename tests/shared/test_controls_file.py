"""Tests for scripts/shared/controls_file.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from shared.controls_file import (  # noqa: E402
    read_controls_json,
    read_controls_json_at,
    resolve_controls_path,
)


def test_resolve_controls_path_honours_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "custom-controls.json"
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(override))
    assert resolve_controls_path() == override


def test_resolve_controls_path_default_repo_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MK04_CONTROLS_FILE", raising=False)
    assert resolve_controls_path() == REPO_ROOT / "ops-ui" / "data" / "controls.json"


def test_read_controls_json_missing_file_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(tmp_path / "missing.json"))
    assert read_controls_json() == {}


def test_read_controls_json_invalid_json_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "controls.json"
    bad.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(bad))
    assert read_controls_json() == {}


def test_read_controls_json_at_loads_dict(tmp_path: Path) -> None:
    path = tmp_path / "controls.json"
    path.write_text(json.dumps({"ingestion_paused": True}), encoding="utf-8")
    assert read_controls_json_at(path) == {"ingestion_paused": True}


def test_read_controls_json_at_non_object_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "controls.json"
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert read_controls_json_at(path) == {}
