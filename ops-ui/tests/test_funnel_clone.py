"""Tests for the Clone Funnel workflow (Funnel Management MK1)."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.funnel_management.clone import clone_canonical_funnel, save_cloned_funnel_in_registry
from ops_ui.funnel_management.clone import FunnelCloneError, FunnelCloneForm, parse_funnel_clone_form
from ops_ui.funnel_management.registry import FunnelRegistry
from ops_ui.funnel_management.schema import CanonicalFunnel, dump_canonical_funnel, load_canonical_funnel


SOURCE_ID = "mfm_business_ai_001"
NEW_ID = "mfm_business_ai_clone_001"
SHARED_PROFILE = "business_podcasts_001"


def _valid_funnel_payload(**identity_overrides: object) -> dict:
    identity = {
        "funnel_id": SOURCE_ID,
        "display_name": "MFM Business AI",
        "description": "Business podcast clipping funnel",
        "category": "business",
        "enabled": True,
        "environment": "prod",
        "status": "active",
        "template_source": None,
        "created_at": "2026-07-04T00:00:00Z",
        "updated_at": "2026-07-04T00:00:00Z",
        "operator_note": "Production note",
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
            "pipeline_profile": SOURCE_ID,
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
                "tiktok": True,
                "instagram_reels": False,
                "facebook_reels": False,
                "x": False,
            },
        },
        "distribution": {
            "posting_enabled": True,
            "posting_mode": "manual_review",
            "target_platforms": ["youtube_shorts", "tiktok"],
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


def _settings(
    tmp_path: Path,
    *,
    auth_enabled: bool = False,
    password: str = "secret-pass",
) -> Settings:
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
        auth_enabled=auth_enabled,
        operator_password=password,
        secret_key="test-secret-key",
        services=(),
    )


def _save_source(registry_dir: Path, **identity_overrides: object) -> CanonicalFunnel:
    funnel = load_canonical_funnel(_valid_funnel_payload(**identity_overrides))
    FunnelRegistry(registry_dir).save_funnel(funnel)
    return funnel


def _login(client, password: str = "secret-pass") -> None:
    page = client.get("/login")
    html = page.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    token = html.split(marker, 1)[1].split('"', 1)[0]
    client.post(
        "/login",
        data={"password": password, "csrf_token": token, "next": f"/funnels/{SOURCE_ID}/clone"},
    )


def _csrf_token(client, funnel_id: str = SOURCE_ID) -> str:
    page = client.get(f"/funnels/{funnel_id}/clone")
    html = page.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    return html.split(marker, 1)[1].split('"', 1)[0]


def _clone_form(**overrides: str) -> dict[str, str]:
    data = {
        "new_funnel_id": NEW_ID,
        "display_name": "MFM Business AI Clone",
        "environment": "dev",
        "description": "Cloned draft",
        "category": "business",
        "operator_note": "Clone note",
        "copy_sources": "on",
        "copy_distribution_routes": "on",
        "copy_mappings": "on",
    }
    data.update(overrides)
    return data


@pytest.fixture
def registry_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir()
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))
    _save_source(registry_dir)
    return registry_dir


class TestCloneHelper:
    def test_clone_creates_valid_funnel(self) -> None:
        source = load_canonical_funnel(_valid_funnel_payload())
        cloned = clone_canonical_funnel(
            source,
            new_funnel_id=NEW_ID,
            display_name="Clone Name",
            environment="dev",
        )
        dump_canonical_funnel(cloned)

    def test_source_not_mutated(self) -> None:
        source = load_canonical_funnel(_valid_funnel_payload())
        before = dump_canonical_funnel(source)
        clone_canonical_funnel(source, new_funnel_id=NEW_ID, display_name="Clone Name")
        after = dump_canonical_funnel(source)
        assert before == after

    def test_safety_overrides(self) -> None:
        source = load_canonical_funnel(_valid_funnel_payload())
        cloned = clone_canonical_funnel(
            source,
            new_funnel_id=NEW_ID,
            display_name="Clone Name",
            environment="dev",
        )
        assert cloned.identity.funnel_id == NEW_ID
        assert cloned.identity.display_name == "Clone Name"
        assert cloned.identity.status == "draft"
        assert cloned.identity.enabled is False
        assert cloned.distribution.posting_enabled is False
        assert cloned.identity.template_source == f"clone:{SOURCE_ID}"
        assert cloned.identity.created_at != source.identity.created_at
        assert cloned.identity.updated_at != source.identity.updated_at

    def test_pipeline_profile_updates_when_matches_source_id(self) -> None:
        source = load_canonical_funnel(_valid_funnel_payload())
        cloned = clone_canonical_funnel(source, new_funnel_id=NEW_ID, display_name="Clone")
        assert cloned.processing.pipeline_profile == NEW_ID

    def test_pipeline_profile_preserved_when_shared(self) -> None:
        payload = _valid_funnel_payload()
        payload["processing"]["pipeline_profile"] = SHARED_PROFILE
        source = load_canonical_funnel(payload)
        cloned = clone_canonical_funnel(source, new_funnel_id=NEW_ID, display_name="Clone")
        assert cloned.processing.pipeline_profile == SHARED_PROFILE

    def test_copy_options(self) -> None:
        source = load_canonical_funnel(_valid_funnel_payload())
        cloned = clone_canonical_funnel(
            source,
            new_funnel_id=NEW_ID,
            display_name="Clone",
            copy_sources=False,
            copy_distribution_routes=False,
            copy_mappings=False,
        )
        assert cloned.acquisition.sources == ()
        assert cloned.distribution.channel_routes == ()
        assert cloned.mappings.config_manager_funnel_id is None

    def test_defaults_copy_config(self) -> None:
        source = load_canonical_funnel(_valid_funnel_payload())
        cloned = clone_canonical_funnel(source, new_funnel_id=NEW_ID, display_name="Clone")
        assert len(cloned.acquisition.sources) == 1
        assert cloned.processing.ai_rules.ai_rule_profile == "business"
        assert cloned.distribution.target_platforms == ("youtube_shorts", "tiktok")
        assert cloned.distribution.channel_routes[0].channel_id == "mfm_business_ai_primary"
        assert cloned.mappings.config_manager_funnel_id == "business"


class TestCloneRoutes:
    def test_get_clone_renders(self, tmp_path: Path, registry_env: Path) -> None:
        response = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{SOURCE_ID}/clone")
        assert response.status_code == 200
        assert b"Clone Funnel" in response.data
        assert SOURCE_ID.encode() in response.data
        assert b'name="new_funnel_id"' in response.data

    def test_missing_source_returns_404(self, tmp_path: Path, registry_env: Path) -> None:
        response = create_app(_settings(tmp_path)).test_client().get("/funnels/missing_funnel/clone")
        assert response.status_code == 404

    def test_post_creates_clone_and_redirects(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _clone_form()
        data["csrf_token"] = _csrf_token(client)
        response = client.post(f"/funnels/{SOURCE_ID}/clone", data=data, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"].endswith(f"/funnels/{NEW_ID}")
        cloned = json.loads((registry_env / f"{NEW_ID}.json").read_text(encoding="utf-8"))
        assert cloned["identity"]["status"] == "draft"
        assert cloned["identity"]["enabled"] is False
        assert cloned["distribution"]["posting_enabled"] is False

    def test_duplicate_new_id_shows_error(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _clone_form()
        data["csrf_token"] = _csrf_token(client)
        client.post(f"/funnels/{SOURCE_ID}/clone", data=data)
        response = client.post(f"/funnels/{SOURCE_ID}/clone", data=data)
        assert response.status_code == 200
        assert b"already exists" in response.data
        assert len(list(registry_env.glob("*.json"))) == 2

    def test_invalid_new_funnel_id(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _clone_form(new_funnel_id="Bad-ID")
        data["csrf_token"] = _csrf_token(client)
        response = client.post(f"/funnels/{SOURCE_ID}/clone", data=data)
        assert response.status_code == 200
        assert not (registry_env / "Bad-ID.json").exists()

    def test_csrf_required_when_auth_enabled(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path, auth_enabled=True)).test_client()
        _login(client)
        response = client.post(f"/funnels/{SOURCE_ID}/clone", data=_clone_form())
        assert response.status_code == 200
        assert b"Invalid security token" in response.data
        assert not (registry_env / f"{NEW_ID}.json").exists()

    def test_get_does_not_create_files(self, tmp_path: Path, registry_env: Path) -> None:
        create_app(_settings(tmp_path)).test_client().get(f"/funnels/{SOURCE_ID}/clone")
        assert len(list(registry_env.glob("*.json"))) == 1


class TestUiIntegration:
    def test_detail_page_has_clone_link(self, tmp_path: Path, registry_env: Path) -> None:
        body = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{SOURCE_ID}").data.decode("utf-8")
        assert f"/funnels/{SOURCE_ID}/clone" in body


class TestScopeProtection:
    def test_source_registry_file_unchanged(self, tmp_path: Path, registry_env: Path) -> None:
        before = (registry_env / f"{SOURCE_ID}.json").read_text(encoding="utf-8")
        client = create_app(_settings(tmp_path)).test_client()
        data = _clone_form()
        data["csrf_token"] = _csrf_token(client)
        client.post(f"/funnels/{SOURCE_ID}/clone", data=data)
        after = (registry_env / f"{SOURCE_ID}.json").read_text(encoding="utf-8")
        assert before == after

    def test_no_runtime_files_written(self, tmp_path: Path, registry_env: Path) -> None:
        runtime = tmp_path / "runtime.json"
        runtime.write_text("{}", encoding="utf-8")
        client = create_app(_settings(tmp_path)).test_client()
        data = _clone_form()
        data["csrf_token"] = _csrf_token(client)
        client.post(f"/funnels/{SOURCE_ID}/clone", data=data)
        assert runtime.read_text(encoding="utf-8") == "{}"

    def test_cloned_funnel_has_no_forbidden_fields(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _clone_form()
        data["csrf_token"] = _csrf_token(client)
        client.post(f"/funnels/{SOURCE_ID}/clone", data=data)
        dumped = json.loads((registry_env / f"{NEW_ID}.json").read_text(encoding="utf-8"))
        forbidden = {"readiness", "operations", "pause_state", "prompt_text", "oauth", "credentials"}
        assert forbidden.isdisjoint(dumped.keys())

    def test_no_edit_sync_delete_buttons_on_clone_form(self, tmp_path: Path, registry_env: Path) -> None:
        body = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{SOURCE_ID}/clone").data.decode("utf-8").lower()
        for forbidden in ("edit funnel", "sync funnel", "delete funnel", "archive funnel"):
            assert forbidden not in body

    def test_same_source_and_new_id_rejected(self) -> None:
        parsed, errors = parse_funnel_clone_form(
            {"new_funnel_id": SOURCE_ID, "display_name": "Same", "environment": "dev"},
            source_funnel_id=SOURCE_ID,
        )
        assert parsed is None
        assert any("differ" in error for error in errors)
