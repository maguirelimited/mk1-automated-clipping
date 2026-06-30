from __future__ import annotations

import json
from pathlib import Path

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.post_processing_config import (
    effective_config as pp_effective,
    parse_form as pp_parse,
    source_for as pp_source,
)
from ops_ui.processing_config import (
    effective_config as proc_effective,
    parse_form as proc_parse,
)
from ops_ui.store import ControlStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=5070,
        data_dir=tmp_path,
        control_db_path=tmp_path / "ops.sqlite3",
        controls_file=tmp_path / "controls.json",
        service_timeout_sec=0.01,
        journal_lines=1,
        funnel_run_timeout_sec=1.0,
        stuck_running_sec=7200.0,
        stuck_queued_sec=1800.0,
        stuck_uploading_sec=1800.0,
        ai_service_url="http://127.0.0.1:9",
        ai_diagnostics_timeout_sec=0.01,
        services=(
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-video-automation.service",
            ),
        ),
    )


def test_store_pipeline_config_roundtrip_and_export(tmp_path: Path) -> None:
    store = ControlStore(tmp_path / "ops.sqlite3", controls_file=tmp_path / "controls.json")
    store.init_db()
    store.set_processing_config({"processing_pipeline_mode": "mk1", "section_overlap_sec": "10.0"})
    store.set_post_processing_config({"selection_mode": "growth", "max_clips": "9"})

    assert store.get_processing_config()["processing_pipeline_mode"] == "mk1"
    assert store.get_post_processing_config()["selection_mode"] == "growth"

    exported = json.loads((tmp_path / "controls.json").read_text(encoding="utf-8"))
    assert exported["processing_config"]["processing_pipeline_mode"] == "mk1"
    assert exported["post_processing_config"]["max_clips"] == "9"
    # The existing flags + ai block remain present alongside the new blocks.
    assert "ingestion_paused" in exported
    assert "ai_config" in exported


def test_processing_effective_resolution(monkeypatch) -> None:
    monkeypatch.delenv("PROCESSING_PIPELINE_MODE", raising=False)
    monkeypatch.setenv("PROCESSING_SECTION_OVERLAP_SEC", "12")
    saved = {"processing_pipeline_mode": "mk1"}
    effective = proc_effective(saved)
    assert effective["processing_pipeline_mode"] == "mk1"  # from saved
    assert effective["section_overlap_sec"] == 12.0  # from env
    assert effective["section_target_duration_sec"] == 300.0  # default


def test_post_processing_bool_and_source(monkeypatch) -> None:
    monkeypatch.delenv("POST_PROCESSING_ENABLED", raising=False)
    saved = {"post_processing_enabled": "false"}
    effective = pp_effective(saved)
    assert effective["post_processing_enabled"] is False
    assert pp_source("post_processing_enabled", saved) == "ui"
    assert pp_source("selection_mode", saved) == "default"


def test_parse_form_validates_modes_and_numbers() -> None:
    values, errors = proc_parse({"processing_pipeline_mode": "mk1", "section_overlap_sec": "5"})
    assert not errors and values["processing_pipeline_mode"] == "mk1"
    _, errors = proc_parse({"processing_pipeline_mode": "bogus"})
    assert errors

    values, errors = pp_parse({"selection_mode": "balanced", "max_clips": "6", "post_processing_enabled": "true"})
    assert not errors and values["max_clips"] == "6"
    _, errors = pp_parse({"selection_mode": "nope"})
    assert errors
    _, errors = pp_parse({"max_clips": "not-a-number"})
    assert errors


def test_settings_page_renders_new_sections(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    html = app.test_client().get("/settings").data.decode()
    assert "Processing pipeline mode" in html
    assert "Selection mode" in html
    assert "Caption font" in html


def test_post_routes_persist(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = app.test_client()
    assert client.post(
        "/settings/processing", data={"processing_pipeline_mode": "mk1"}
    ).status_code in (301, 302)
    assert client.post(
        "/settings/post-processing", data={"selection_mode": "maximum_quality"}
    ).status_code in (301, 302)
    exported = json.loads((tmp_path / "controls.json").read_text(encoding="utf-8"))
    assert exported["processing_config"]["processing_pipeline_mode"] == "mk1"
    assert exported["post_processing_config"]["selection_mode"] == "maximum_quality"
