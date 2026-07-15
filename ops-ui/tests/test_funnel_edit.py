"""Tests for the Edit Funnel workflow (Funnel Management MK1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops_ui.app import create_app
from ops_ui.config import Settings
from ops_ui.funnel_management.edit import edit_form_from_funnel, update_funnel_from_form
from ops_ui.funnel_management.registry import FunnelRegistry
from ops_ui.funnel_management.schema import ALLOWED_PLATFORMS, dump_canonical_funnel, load_canonical_funnel


FUNNEL_ID = "mfm_business_ai_001"


def _valid_funnel_payload(**identity_overrides: object) -> dict:
    identity = {
        "funnel_id": FUNNEL_ID,
        "display_name": "MFM Business AI",
        "description": "Business podcast clipping funnel",
        "category": "business",
        "enabled": True,
        "environment": "prod",
        "status": "active",
        "template_source": "clone:source_001",
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
                    "title_allowlist": ["MFM"],
                    "title_blocklist": ["Short"],
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


def _save_funnel(registry_dir: Path, **identity_overrides: object) -> None:
    funnel = load_canonical_funnel(_valid_funnel_payload(**identity_overrides))
    FunnelRegistry(registry_dir).save_funnel(funnel)


def _login(client, password: str = "secret-pass") -> None:
    page = client.get("/login")
    html = page.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    token = html.split(marker, 1)[1].split('"', 1)[0]
    client.post(
        "/login",
        data={"password": password, "csrf_token": token, "next": f"/funnels/{FUNNEL_ID}/edit"},
    )


def _csrf_token(client) -> str:
    page = client.get(f"/funnels/{FUNNEL_ID}/edit")
    html = page.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    return html.split(marker, 1)[1].split('"', 1)[0]


def _form_to_post(form: dict, **overrides: str) -> dict[str, str]:
    data: dict[str, str] = {
        "funnel_id": form["funnel_id"],
        "display_name": form["display_name"],
        "description": form["description"],
        "category": form["category"],
        "status": form["status"],
        "environment": form["environment"],
        "operator_note": form["operator_note"],
        "created_at": form["created_at"],
        "template_source": form["template_source"],
        "acquisition_source_type": form["acquisition_source_type"],
        "min_duration_minutes": form["min_duration_minutes"],
        "max_duration_minutes": form["max_duration_minutes"],
        "max_downloads_per_run": form["max_downloads_per_run"],
        "pipeline_profile": form["pipeline_profile"],
        "ai_rule_profile": form["ai_rule_profile"],
        "max_clips": form["max_clips"],
        "min_clip_duration_sec": form["min_clip_duration_sec"],
        "max_clip_duration_sec": form["max_clip_duration_sec"],
        "max_overlap_sec": form["max_overlap_sec"],
        "filename_prefix": form["filename_prefix"],
        "delivery_mode": form["delivery_mode"],
        "posting_mode": form["posting_mode"],
        "config_manager_funnel_id": form["config_manager_funnel_id"],
        "source_count": str(len(form["sources"])),
        "route_count": str(len(form["routes"])),
    }
    if form.get("enabled"):
        data["enabled"] = "on"
    if form.get("posting_enabled"):
        data["posting_enabled"] = "on"

    for index, source in enumerate(form["sources"]):
        prefix = f"source_{index}_"
        data[f"{prefix}source_id"] = source["source_id"]
        data[f"{prefix}label"] = source["label"]
        data[f"{prefix}url"] = source["url"]
        data[f"{prefix}source_type"] = source["source_type"]
        data[f"{prefix}max_videos_per_source"] = source["max_videos_per_source"]
        data[f"{prefix}title_allowlist"] = source["title_allowlist"]
        data[f"{prefix}title_blocklist"] = source["title_blocklist"]
        if source.get("active"):
            data[f"{prefix}active"] = "on"
        if source.get("hydrate_missing_duration"):
            data[f"{prefix}hydrate_missing_duration"] = "on"
        if source.get("remove"):
            data[f"{prefix}remove"] = "on"

    for index, route in enumerate(form["routes"]):
        prefix = f"route_{index}_"
        data[f"{prefix}channel_id"] = route["channel_id"]
        data[f"{prefix}platform"] = route["platform"]
        if route.get("enabled"):
            data[f"{prefix}enabled"] = "on"
        if route.get("remove"):
            data[f"{prefix}remove"] = "on"

    for platform in sorted(ALLOWED_PLATFORMS):
        if form["platforms"].get(platform):
            data[f"platform_{platform}"] = "on"
        if form["target_platforms"].get(platform):
            data[f"target_platform_{platform}"] = "on"

    new_source = form.get("new_source", {})
    data["new_source_source_id"] = new_source.get("source_id", "")
    data["new_source_label"] = new_source.get("label", "")
    data["new_source_url"] = new_source.get("url", "")
    data["new_source_source_type"] = new_source.get("source_type", "")

    new_route = form.get("new_route", {})
    if str(new_route.get("channel_id") or "").strip():
        data["new_route_channel_id"] = new_route.get("channel_id", "")
        data["new_route_platform"] = new_route.get("platform", "youtube_shorts")
        if new_route.get("enabled"):
            data["new_route_enabled"] = "on"

    data.update(overrides)
    return data


def _edit_post(client, registry_dir: Path, **overrides: str) -> dict[str, str]:
    funnel = FunnelRegistry(registry_dir).get_funnel(FUNNEL_ID)
    form = edit_form_from_funnel(funnel)
    data = _form_to_post(form, **overrides)
    data["csrf_token"] = _csrf_token(client)
    return data


@pytest.fixture
def registry_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir()
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))
    _save_funnel(registry_dir)
    return registry_dir


class TestGetEditForm:
    def test_get_renders(self, tmp_path: Path, registry_env: Path) -> None:
        response = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}/edit")
        assert response.status_code == 200
        body = response.data
        assert b"Edit Funnel" in body
        assert b'name="display_name"' in body
        assert b'name="ai_rule_profile"' in body
        assert b'name="config_manager_funnel_id"' in body
        assert b'name="csrf_token"' in body

    def test_missing_funnel_returns_404(self, tmp_path: Path, registry_env: Path) -> None:
        response = create_app(_settings(tmp_path)).test_client().get("/funnels/missing/edit")
        assert response.status_code == 404

    def test_form_shows_sections(self, tmp_path: Path, registry_env: Path) -> None:
        body = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}/edit").data.decode("utf-8")
        assert "Acquisition" in body
        assert "Processing" in body
        assert "Distribution" in body
        assert "my_first_million" in body
        assert "mfm_business_ai_primary" in body


class TestSuccessfulSave:
    def test_post_updates_registry_and_redirects(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(client, registry_env, display_name="Updated Name")
        response = client.post(f"/funnels/{FUNNEL_ID}/edit", data=data, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"].endswith(f"/funnels/{FUNNEL_ID}")
        saved = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        assert saved["identity"]["display_name"] == "Updated Name"

    def test_immutable_fields_preserved(self, tmp_path: Path, registry_env: Path) -> None:
        before = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(
            client,
            registry_env,
            display_name="Changed",
            funnel_id="attempted_new_id",
        )
        response = client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        assert response.status_code == 200
        after = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        assert after["identity"]["funnel_id"] == FUNNEL_ID
        assert after["identity"]["created_at"] == before["identity"]["created_at"]
        assert after["identity"]["template_source"] == before["identity"]["template_source"]
        assert after["identity"]["display_name"] == before["identity"]["display_name"]
        assert b"Funnel ID cannot be changed" in response.data

    def test_identity_fields_update(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(
            client,
            registry_env,
            description="New description",
            category="tech",
            status="draft",
            environment="dev",
            operator_note="Edited note",
        )
        data["enabled"] = ""
        client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        saved = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        assert saved["identity"]["description"] == "New description"
        assert saved["identity"]["category"] == "tech"
        assert saved["identity"]["status"] == "draft"
        assert saved["identity"]["environment"] == "dev"
        assert saved["identity"]["enabled"] is False
        assert saved["identity"]["operator_note"] == "Edited note"


class TestAcquisitionEditing:
    def test_acquisition_fields_update(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(
            client,
            registry_env,
            acquisition_source_type="youtube_playlist",
            min_duration_minutes="10",
            max_duration_minutes="120",
            max_downloads_per_run="3",
            source_0_label="Updated Label",
            source_0_title_allowlist="Alpha\nBeta, Gamma",
            source_0_title_blocklist="Noise",
        )
        client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        saved = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        assert saved["acquisition"]["source_type"] == "youtube_playlist"
        assert saved["acquisition"]["min_duration_minutes"] == 10
        assert saved["acquisition"]["max_duration_minutes"] == 120
        assert saved["acquisition"]["max_downloads_per_run"] == 3
        assert saved["acquisition"]["sources"][0]["label"] == "Updated Label"
        assert saved["acquisition"]["sources"][0]["title_allowlist"] == ["Alpha", "Beta", "Gamma"]
        assert saved["acquisition"]["sources"][0]["title_blocklist"] == ["Noise"]

    def test_add_new_source(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(
            client,
            registry_env,
            new_source_source_id="extra_source",
            new_source_label="Extra Source",
            new_source_url="https://www.youtube.com/@Extra",
            new_source_source_type="youtube_channel",
        )
        client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        saved = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        source_ids = [source["source_id"] for source in saved["acquisition"]["sources"]]
        assert "extra_source" in source_ids

    def test_remove_source(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(client, registry_env, source_0_remove="on")
        client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        saved = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        assert saved["acquisition"]["sources"] == []


class TestProcessingEditing:
    def test_processing_fields_update(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(
            client,
            registry_env,
            pipeline_profile="shared_profile_001",
            ai_rule_profile="business",
            max_clips="8",
            min_clip_duration_sec="15",
            max_clip_duration_sec="60",
            max_overlap_sec="2",
            filename_prefix="updated_prefix",
            delivery_mode="pull_from_output_endpoint",
            platform_instagram_reels="on",
        )
        client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        saved = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        assert saved["processing"]["pipeline_profile"] == "shared_profile_001"
        assert saved["processing"]["ai_rules"]["ai_rule_profile"] == "business"
        assert saved["processing"]["selection"]["max_clips"] == 8
        assert saved["processing"]["output"]["filename_prefix"] == "updated_prefix"
        assert saved["processing"]["platforms"]["instagram_reels"] is True
        assert "prompt_text" not in saved["processing"]["ai_rules"]


class TestCustomAiRulesPreservation:
    def _save_custom_funnel(self, registry_dir: Path) -> None:
        payload = _valid_funnel_payload()
        payload["processing"]["ai_rules"] = {
            "ai_rule_profile": "gaming",
            "prompt_managed": "custom",
            "prompt_text": "Select chaotic gaming highlights.",
        }
        payload["mappings"]["config_manager_preset_id"] = "growth"
        FunnelRegistry(registry_dir).save_funnel(load_canonical_funnel(payload), overwrite=True)

    def test_edit_preserves_custom_prompt_and_preset(self, tmp_path: Path, registry_env: Path) -> None:
        self._save_custom_funnel(registry_env)
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(client, registry_env, display_name="GTA Clips Updated", max_clips="6")
        client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        saved = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        ai_rules = saved["processing"]["ai_rules"]
        assert ai_rules["prompt_managed"] == "custom"
        assert ai_rules["prompt_text"] == "Select chaotic gaming highlights."
        assert saved["mappings"]["config_manager_preset_id"] == "growth"
        assert saved["identity"]["display_name"] == "GTA Clips Updated"


class TestDistributionEditing:
    def test_distribution_fields_update(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(
            client,
            registry_env,
            posting_mode="auto_queue",
            route_0_channel_id="updated_channel",
            route_0_platform="tiktok",
        )
        data["posting_enabled"] = "on"
        data["target_platform_tiktok"] = "on"
        client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        saved = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        assert saved["distribution"]["posting_enabled"] is True
        assert saved["distribution"]["posting_mode"] == "auto_queue"
        assert "tiktok" in saved["distribution"]["target_platforms"]
        assert saved["distribution"]["channel_routes"][0]["channel_id"] == "updated_channel"
        assert saved["distribution"]["channel_routes"][0]["platform"] == "tiktok"


class TestMappingsEditing:
    def test_mapping_updates_and_blank_becomes_null(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(client, registry_env, config_manager_funnel_id="new_mapping")
        client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        saved = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        assert saved["mappings"]["config_manager_funnel_id"] == "new_mapping"

        data = _edit_post(client, registry_env, config_manager_funnel_id="")
        client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        saved = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        assert saved["mappings"]["config_manager_funnel_id"] is None


class TestValidationAndErrors:
    def test_invalid_display_name_does_not_save(self, tmp_path: Path, registry_env: Path) -> None:
        before = (registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8")
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(client, registry_env, display_name="")
        response = client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        assert response.status_code == 200
        assert (registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8") == before

    def test_invalid_duration_bounds(self, tmp_path: Path, registry_env: Path) -> None:
        before = (registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8")
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(client, registry_env, min_duration_minutes="200", max_duration_minutes="100")
        response = client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        assert response.status_code == 200
        assert (registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8") == before

    def test_invalid_clip_duration_bounds(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(client, registry_env, min_clip_duration_sec="90", max_clip_duration_sec="20")
        response = client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        assert response.status_code == 200

    def test_invalid_posting_mode(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(client, registry_env, posting_mode="invalid_mode")
        response = client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        assert response.status_code == 200

    def test_invalid_platform_on_route(self, tmp_path: Path, registry_env: Path) -> None:
        before = (registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8")
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(client, registry_env, route_0_platform="invalid_platform")
        response = client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        assert response.status_code == 200
        assert (registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8") == before

    def test_funnel_id_change_rejected(self, tmp_path: Path, registry_env: Path) -> None:
        existing = FunnelRegistry(registry_env).get_funnel(FUNNEL_ID)
        updated, errors = update_funnel_from_form(existing, {"funnel_id": "other_id", "display_name": "X"})
        assert updated is None
        assert any("cannot be changed" in error for error in errors)

    def test_csrf_required_when_auth_enabled(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path, auth_enabled=True)).test_client()
        _login(client)
        data = _form_to_post(edit_form_from_funnel(FunnelRegistry(registry_env).get_funnel(FUNNEL_ID)))
        response = client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        assert response.status_code == 200
        assert b"Invalid security token" in response.data


class TestUiIntegration:
    def test_detail_page_has_edit_link(self, tmp_path: Path, registry_env: Path) -> None:
        body = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}").data.decode("utf-8")
        assert f"/funnels/{FUNNEL_ID}/edit" in body


class TestScopeProtection:
    def test_no_runtime_files_written(self, tmp_path: Path, registry_env: Path) -> None:
        runtime = tmp_path / "runtime.json"
        runtime.write_text("{}", encoding="utf-8")
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(client, registry_env, display_name="Scoped")
        client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        assert runtime.read_text(encoding="utf-8") == "{}"

    def test_no_forbidden_fields_persisted(self, tmp_path: Path, registry_env: Path) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        data = _edit_post(client, registry_env, display_name="Clean Save")
        client.post(f"/funnels/{FUNNEL_ID}/edit", data=data)
        saved = json.loads((registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        forbidden = {"readiness", "operations", "pause_state", "prompt_text", "oauth", "credentials"}
        assert forbidden.isdisjoint(saved.keys())
        assert "prompt_text" not in saved.get("processing", {}).get("ai_rules", {})

    def test_no_sync_delete_archive_buttons(self, tmp_path: Path, registry_env: Path) -> None:
        body = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}/edit").data.decode("utf-8").lower()
        for forbidden in ("sync funnel", "delete funnel", "archive funnel"):
            assert forbidden not in body

    def test_existing_object_not_mutated(self) -> None:
        existing = load_canonical_funnel(_valid_funnel_payload())
        before = dump_canonical_funnel(existing)
        form = edit_form_from_funnel(existing)
        post = _form_to_post(form, display_name="Mutate Check")
        update_funnel_from_form(existing, post)
        after = dump_canonical_funnel(existing)
        assert before == after

    def test_get_does_not_modify_registry(self, tmp_path: Path, registry_env: Path) -> None:
        before = (registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8")
        create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}/edit")
        assert (registry_env / f"{FUNNEL_ID}.json").read_text(encoding="utf-8") == before
