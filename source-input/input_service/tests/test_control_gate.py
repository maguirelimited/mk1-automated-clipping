from __future__ import annotations

import json
from pathlib import Path

from input_service.control_gate import ingestion_paused, read_controls


def test_ingestion_paused_reads_controls_file(tmp_path: Path, monkeypatch) -> None:
    controls = tmp_path / "controls.json"
    controls.write_text(
        json.dumps({"ingestion_paused": True, "uploads_paused": False}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(controls))
    assert read_controls()["ingestion_paused"] is True
    assert ingestion_paused() is True
