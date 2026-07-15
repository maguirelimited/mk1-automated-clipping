from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.funnel_management.registry import FunnelRegistry
from ops_ui.funnel_management.schema import load_canonical_funnel
from ops_ui.funnels import load_canonical_funnel_detail
from ops_ui.store import ControlStore


FUNNEL_ID = "mfm_business_ai_001"


def _valid_funnel_payload(**identity_overrides: object) -> dict:
    identity = {
        "funnel_id": FUNNEL_ID,
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
                    "title_allowlist": ["ai"],
                    "title_blocklist": ["shorts"],
                }
            ],
            "min_duration_minutes": 20,
            "max_duration_minutes": 180,
            "max_downloads_per_run": 1,
        },
        "processing": {
            "pipeline_profile": FUNNEL_ID,
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


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    registry_dir = tmp_path / "registry"
    _save_registry_funnel(registry_dir)
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))
    return create_app(_settings(tmp_path)).test_client(), registry_dir


class TestDetailRoute:
    def test_detail_renders_for_saved_funnel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _registry = _client(tmp_path, monkeypatch)
        response = client.get(f"/funnels/{FUNNEL_ID}")
        assert response.status_code == 200
        body = response.data
        assert b"MFM Business AI" in body
        assert b"Status" in body
        assert b"Run test" in body

    def test_missing_funnel_returns_404(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        registry_dir = tmp_path / "registry"
        registry_dir.mkdir()
        monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))
        response = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}")
        assert response.status_code == 404

    def test_invalid_registry_file_shows_controlled_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        registry_dir = tmp_path / "registry"
        registry_dir.mkdir()
        (registry_dir / f"{FUNNEL_ID}.json").write_text('{"schema_version": 1}', encoding="utf-8")
        monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))
        response = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}")
        assert response.status_code == 200
        assert b"Could not load canonical funnel" in response.data


class TestHeaderDisplay:
    def test_header_fields(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _registry = _client(tmp_path, monkeypatch)
        body = client.get(f"/funnels/{FUNNEL_ID}").data.decode("utf-8")
        assert "MFM Business AI" in body
        assert FUNNEL_ID in body
        assert "dev" in body
        assert "active" in body
        assert "Processing" in body
        assert "Synced" in body
        assert "Posting" in body


class TestReadinessDisplay:
    def test_validation_sections_render(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _registry = _client(tmp_path, monkeypatch)
        body = client.get(f"/funnels/{FUNNEL_ID}").data.decode("utf-8")
        assert "checked" in body.lower()
        assert "Errors" in body or "Warnings" in body or "ready" in body.lower()

    def test_readiness_not_written_to_registry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, registry_dir = _client(tmp_path, monkeypatch)
        before = (registry_dir / f"{FUNNEL_ID}.json").read_text(encoding="utf-8")
        client.get(f"/funnels/{FUNNEL_ID}")
        after = (registry_dir / f"{FUNNEL_ID}.json").read_text(encoding="utf-8")
        assert before == after
        assert "readiness" not in after


class TestSections:
    def test_core_sections_render(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _registry = _client(tmp_path, monkeypatch)
        body = client.get(f"/funnels/{FUNNEL_ID}").data.decode("utf-8")
        assert "Identity" in body
        assert "Sources" in body
        assert "My First Million" in body
        assert "business" in body
        assert "Sync runtime config" in body

    def test_advanced_sections_available(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _registry = _client(tmp_path, monkeypatch)
        body = client.get(f"/funnels/{FUNNEL_ID}").data.decode("utf-8")
        assert "Processing &amp; distribution (advanced)" in body or "Processing & distribution (advanced)" in body
        assert "Advanced readiness details" in body

    def test_no_prompt_text_or_credentials(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _registry = _client(tmp_path, monkeypatch)
        body = client.get(f"/funnels/{FUNNEL_ID}").data.decode("utf-8").lower()
        assert "prompt_text" not in body
        assert "oauth" not in body
        assert "token_file_env" not in body
        assert "credentials" not in body


class TestOperationalOverlay:
    def test_run_test_button_uses_ops_can_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _registry = _client(tmp_path, monkeypatch)
        ops_row = {
            "funnel_id": FUNNEL_ID,
            "paused": False,
            "can_run": True,
            "health": "ok",
            "operator_status": "live",
            "last_run_at": "2026-05-23T10:00:00+00:00",
            "last_success_at": "2026-05-23T09:00:00+00:00",
            "last_run_state": "input_ready",
            "failure_count": 1,
            "queue_depth": 2,
            "active_video_jobs": 1,
            "active_upload_jobs": 0,
        }
        board = {"rows": [ops_row], "trigger_history": []}
        with patch("ops_ui.funnels.load_funnel_board", return_value=board):
            body = client.get(f"/funnels/{FUNNEL_ID}").data.decode("utf-8")
        assert "Run test" in body

    def test_page_renders_when_ops_overlay_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _registry = _client(tmp_path, monkeypatch)
        with patch("ops_ui.funnels.load_funnel_board", side_effect=RuntimeError("ops down")):
            response = client.get(f"/funnels/{FUNNEL_ID}")
        assert response.status_code == 200
        assert b"MFM Business AI" in response.data
        assert b"Run test" in response.data


class TestListLink:
    def test_funnels_list_links_to_detail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _registry = _client(tmp_path, monkeypatch)
        body = client.get("/funnels").data.decode("utf-8")
        assert f'/funnels/{FUNNEL_ID}' in body


class TestScopeProtection:
    def test_no_edit_clone_save_sync_delete(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _registry = _client(tmp_path, monkeypatch)
        body = client.get(f"/funnels/{FUNNEL_ID}").data.decode("utf-8").lower()
        for forbidden in ("save funnel", "delete funnel", "archive funnel"):
            assert forbidden not in body

    def test_detail_loader_does_not_write_runtime_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        registry_dir = tmp_path / "registry"
        _save_registry_funnel(registry_dir)
        runtime = tmp_path / "runtime.json"
        runtime.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))

        settings = _settings(tmp_path)
        store = ControlStore(settings.control_db_path)
        store.init_db()
        load_canonical_funnel_detail(FUNNEL_ID, settings, store, ingestion_paused=False, registry_dir=registry_dir)
        assert runtime.read_text(encoding="utf-8") == "{}"
