from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.funnel_management.registry import FunnelRegistry
from ops_ui.funnel_management.schema import load_canonical_funnel
from ops_ui.funnels import (
    build_canonical_funnel_list_rows,
    build_funnel_validator,
    build_funnel_rows,
    load_canonical_funnel_page,
    load_console_funnel_context,
    scan_input_ledger,
)
from ops_ui.store import ControlStore


def _valid_funnel_payload(**identity_overrides: object) -> dict:
    identity = {
        "funnel_id": "mfm_business_ai_001",
        "display_name": "MFM Business AI",
        "description": "Business podcast clipping funnel",
        "category": "business",
        "enabled": True,
        "environment": "dev",
        "status": "active",
        "template_source": None,
        "created_at": "2026-07-04T00:00:00Z",
        "updated_at": "2026-07-04T00:00:00Z",
        "operator_note": None,
    }
    identity.update(identity_overrides)
    return {
        "schema_version": 1,
        "identity": identity,
        "acquisition": {
            "source_type": "youtube_channel",
            "sources": [
                {
                    "source_id": "my_first_million",
                    "label": "My First Million",
                    "url": "https://www.youtube.com/@MyFirstMillionPod",
                    "source_type": "youtube_channel",
                    "active": True,
                    "max_videos_per_source": 5,
                    "hydrate_missing_duration": True,
                    "title_allowlist": [],
                    "title_blocklist": [],
                }
            ],
            "min_duration_minutes": 20,
            "max_duration_minutes": 180,
            "max_downloads_per_run": 1,
        },
        "processing": {
            "pipeline_profile": "mfm_business_ai_001",
            "ai_rules": {"ai_rule_profile": "business"},
            "selection": {
                "max_clips": 6,
                "min_clip_duration_sec": 20,
                "max_clip_duration_sec": 90,
                "max_overlap_sec": 5,
            },
            "output": {
                "filename_prefix": "mfm_business_ai",
                "delivery_mode": "handoff",
            },
            "platforms": {
                "youtube_shorts": True,
                "tiktok": False,
                "instagram_reels": False,
                "facebook_reels": False,
                "x": False,
            },
        },
        "distribution": {
            "posting_enabled": False,
            "posting_mode": "manual_review",
            "target_platforms": ["youtube_shorts"],
            "channel_routes": [
                {
                    "channel_id": "mfm_business_ai_primary",
                    "platform": "youtube_shorts",
                    "enabled": True,
                }
            ],
        },
        "mappings": {"config_manager_funnel_id": "business"},
    }


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
        services=(
            ServiceConfig(
                key="source-input",
                label="source-input",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-source-input.service",
            ),
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-video-automation.service",
            ),
            ServiceConfig(
                key="output-funnel",
                label="output-funnel",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-output-funnel.service",
            ),
        ),
    )


def _save_registry_funnel(registry_dir: Path, **identity_overrides: object) -> None:
    funnel = load_canonical_funnel(_valid_funnel_payload(**identity_overrides))
    FunnelRegistry(registry_dir).save_funnel(funnel)


def test_funnels_page_renders_empty_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir()
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))

    app = create_app(_settings(tmp_path))
    response = app.test_client().get("/funnels")
    assert response.status_code == 200
    assert b"Funnel Management" in response.data
    assert b"No canonical funnels have been imported yet" in response.data


def test_funnels_page_renders_missing_registry_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing_registry"
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(missing))

    app = create_app(_settings(tmp_path))
    response = app.test_client().get("/funnels")
    assert response.status_code == 200
    assert b"No canonical funnels have been imported yet" in response.data


def test_funnels_page_shows_canonical_funnel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_dir = tmp_path / "registry"
    _save_registry_funnel(registry_dir)
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))

    app = create_app(_settings(tmp_path))
    response = app.test_client().get("/funnels")
    body = response.data

    assert response.status_code == 200
    assert b"mfm_business_ai_001" in body
    assert b"MFM Business AI" in body
    assert b"dev" in body
    assert b"active" in body
    assert b"youtube_shorts" in body
    assert b"Canonical funnels" in body


def test_funnels_page_shows_readiness_separately_from_ops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_dir = tmp_path / "registry"
    _save_registry_funnel(registry_dir)
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))

    app = create_app(_settings(tmp_path))
    response = app.test_client().get("/funnels")
    body = response.data.decode("utf-8")

    # Approved Funnel Management labels (Status / Processing / Operational).
    assert ">Status<" in body or "Status</th>" in body
    assert ">Processing<" in body or "Processing</th>" in body
    assert "Operational" in body
    # Processing readiness must remain a distinct column from operational overlay.
    assert body.index("Processing</th>") < body.index("Operations</th>")
    assert "incomplete" in body or "warning" in body or "ready" in body or "Ready" in body
    # Do not reintroduce the obsolete architecture-heavy heading.
    assert "Readiness</th>" not in body


def test_funnels_page_survives_operational_board_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_dir = tmp_path / "registry"
    _save_registry_funnel(registry_dir)
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))

    settings = _settings(tmp_path)
    app = create_app(settings)
    with patch("ops_ui.funnels.load_funnel_board", side_effect=RuntimeError("ops down")):
        response = app.test_client().get("/funnels")

    assert response.status_code == 200
    assert b"mfm_business_ai_001" in response.data
    assert b"Operational overlay unavailable" in response.data


def test_canonical_list_row_fields(tmp_path: Path) -> None:
    registry_dir = tmp_path / "registry"
    _save_registry_funnel(registry_dir)
    store = ControlStore(tmp_path / "ops.sqlite3")
    store.init_db()

    rows = build_canonical_funnel_list_rows(
        registry=FunnelRegistry(registry_dir),
        validator=build_funnel_validator(),
        store=store,
        ingestion_paused=False,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["funnel_id"] == "mfm_business_ai_001"
    assert row["display_name"] == "MFM Business AI"
    assert row["environment"] == "dev"
    assert row["status"] == "active"
    assert row["enabled"] is True
    assert row["source_count"] == 1
    assert row["target_platforms"] == ["youtube_shorts"]
    assert row["route_count"] == 1
    assert row["readiness_status"] in {"ready", "warning", "incomplete", "invalid"}
    assert "error_count" in row
    assert "warning_count" in row


def test_canonical_list_merges_operational_overlay(tmp_path: Path) -> None:
    registry_dir = tmp_path / "registry"
    _save_registry_funnel(registry_dir)
    store = ControlStore(tmp_path / "ops.sqlite3")
    store.init_db()

    ops_rows = {
        "mfm_business_ai_001": {
            "funnel_id": "mfm_business_ai_001",
            "paused": True,
            "can_run": False,
            "health": "paused",
            "last_run_at": "2026-05-23T10:00:00+00:00",
            "last_success_at": "2026-05-23T09:00:00+00:00",
            "failure_count": 2,
            "queue_depth": 3,
            "active_video_jobs": 1,
            "active_upload_jobs": 0,
        }
    }
    rows = build_canonical_funnel_list_rows(
        registry=FunnelRegistry(registry_dir),
        validator=build_funnel_validator(),
        store=store,
        ingestion_paused=False,
        ops_rows_by_id=ops_rows,
    )
    assert rows[0]["ops"]["available"] is True
    assert rows[0]["ops"]["paused"] is True
    assert rows[0]["ops"]["failure_count"] == 2
    assert rows[0]["ops"]["queue_depth"] == 3


def test_scope_protection_no_edit_or_sync_buttons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_dir = tmp_path / "registry"
    _save_registry_funnel(registry_dir)
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))

    response = create_app(_settings(tmp_path)).test_client().get("/funnels")
    body = response.data.decode("utf-8").lower()
    for forbidden in ("edit funnel", "clone funnel", "sync funnel", "save funnel", "delete funnel"):
        assert forbidden not in body


def test_scope_protection_validation_not_written_to_registry(tmp_path: Path) -> None:
    registry_dir = tmp_path / "registry"
    _save_registry_funnel(registry_dir)
    before = (registry_dir / "mfm_business_ai_001.json").read_text(encoding="utf-8")

    settings = _settings(tmp_path)
    store = ControlStore(settings.control_db_path)
    store.init_db()
    load_canonical_funnel_page(settings, store, ingestion_paused=False, registry_dir=registry_dir)

    after = (registry_dir / "mfm_business_ai_001.json").read_text(encoding="utf-8")
    assert before == after
    assert "readiness" not in after


def test_build_funnel_rows_aggregates_ledger_and_jobs(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    path = ledger / "input_20260523T100000Z_demo.json"
    path.write_text(
        json.dumps(
            {
                "input_id": "input_20260523T100000Z_demo",
                "funnel_id": "demo_funnel",
                "created_at": "2026-05-23T10:00:00+00:00",
                "state": "input_ready",
                "title": "Episode 1",
            }
        ),
        encoding="utf-8",
    )

    store = ControlStore(tmp_path / "ops.sqlite3")
    store.init_db()

    input_map, runs = scan_input_ledger(ledger)
    rows = build_funnel_rows(
        settings=_settings(tmp_path),
        store=store,
        source_funnels=[
            {
                "funnel_id": "demo_funnel",
                "active": True,
                "angle": "test",
                "pipeline_profile": "demo_funnel",
                "source_type": "youtube_channels",
                "sources": [{"label": "Chan", "url": "https://example.com", "active": True}],
                "posting_config": {"platforms": ["youtube_shorts"]},
            }
        ],
        video_jobs=[
            {
                "job_id": "job_1",
                "input_id": "input_20260523T100000Z_demo",
                "status": "running",
            }
        ],
        upload_jobs=[
            {"id": 1, "funnel_id": "demo_funnel", "status": "planned"},
            {"id": 2, "funnel_id": "demo_funnel", "status": "failed_upload"},
        ],
        ingestion_paused=False,
        input_funnel_map=input_map,
        input_runs=runs,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["funnel_id"] == "demo_funnel"
    assert row["active_video_jobs"] == 1
    assert row["queue_depth"] == 1
    assert row["failure_count"] == 1
    assert row["last_success_at"] == "2026-05-23T10:00:00+00:00"
    assert len(row["active_sources"]) == 1


def test_pause_funnel_blocks_run(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = app.test_client()
    pause = client.post("/funnels/demo_funnel/pause")
    assert pause.status_code == 302
    run = client.post("/funnels/run", data={"funnel_id": "demo_funnel", "next": "funnels"})
    assert run.status_code == 302
    with client.session_transaction() as sess:
        flashes = sess.get("_flashes", [])
    assert any("paused" in str(msg).lower() for _cat, msg in flashes)


def test_load_console_funnel_context_uses_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_dir = tmp_path / "registry"
    _save_registry_funnel(registry_dir)
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))

    settings = _settings(tmp_path)
    store = ControlStore(settings.control_db_path)
    store.init_db()
    ctx = load_console_funnel_context(settings, store, ingestion_paused=False)

    assert ctx["console_default_funnel_id"] == "mfm_business_ai_001"
    assert len(ctx["console_funnel_options"]) == 1
    assert ctx["console_funnel_options"][0]["selected"] is True
    assert ctx["console_funnel_rows"][0]["display_name"] == "MFM Business AI"
    assert ctx["console_funnels_empty"] is False


def test_load_console_funnel_context_marks_ingestion_paused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_dir = tmp_path / "registry"
    _save_registry_funnel(registry_dir)
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))

    settings = _settings(tmp_path)
    store = ControlStore(settings.control_db_path)
    store.init_db()
    ctx = load_console_funnel_context(settings, store, ingestion_paused=True)

    assert ctx["console_ingestion_paused"] is True
    assert ctx["console_funnel_options"][0]["disabled"] is True
    assert ctx["console_funnel_options"][0]["disabled_hint"] == "ingestion paused"


def test_funnels_page_returns_200_when_ai_registry_permission_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable AI registry must surface as validation error, not HTTP 500."""
    registry_dir = tmp_path / "registry"
    _save_registry_funnel(registry_dir)

    ai_registry = tmp_path / "funnel_rule_registry.json"
    ai_registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profiles": {
                    "business": {"rules_version": "business_v1", "managed": "builtin"},
                },
                "aliases": {"mfm_business_ai_001": "business"},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(ai_registry, 0o000)
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))
    monkeypatch.setenv("AI_FUNNEL_RULE_REGISTRY", str(ai_registry))

    store = ControlStore(tmp_path / "ops.sqlite3")
    store.init_db()
    try:
        response = create_app(_settings(tmp_path)).test_client().get("/funnels")
        rows = build_canonical_funnel_list_rows(
            registry=FunnelRegistry(registry_dir),
            validator=build_funnel_validator(),
            store=store,
            ingestion_paused=False,
        )
    finally:
        os.chmod(ai_registry, 0o644)

    assert response.status_code == 200
    assert b"mfm_business_ai_001" in response.data
    assert rows[0]["error_count"] >= 1
    assert rows[0]["sync_ready"] is False
    assert rows[0]["readiness_status"] in {"incomplete", "invalid"}
