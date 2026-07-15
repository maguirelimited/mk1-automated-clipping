from __future__ import annotations

import importlib
import json
from pathlib import Path

import input_service.control_gate as control_gate
from input_service.control_gate import ingestion_paused, read_controls


def test_control_gate_import_resolves_to_package_module() -> None:
    module_path = Path(control_gate.__file__).resolve()
    assert module_path.name == "control_gate.py"
    assert module_path.parent.name == "input_service"
    assert importlib.import_module("input_service.control_gate") is control_gate


def test_ingestion_paused_reads_controls_file(tmp_path: Path, monkeypatch) -> None:
    controls = tmp_path / "controls.json"
    controls.write_text(
        json.dumps({"ingestion_paused": True, "uploads_paused": False}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(controls))
    assert read_controls()["ingestion_paused"] is True
    assert ingestion_paused() is True
