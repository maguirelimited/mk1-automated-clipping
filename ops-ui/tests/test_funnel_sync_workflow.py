"""Tests for the Save & Synchronisation workflow (Funnel Management MK1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops_ui.app import create_app
from ops_ui.config import Settings
from ops_ui.funnel_management.registry import FunnelRegistry
from ops_ui.funnel_management.schema import load_canonical_funnel
from ops_ui.funnel_management.sync_workflow import resolve_sync_paths
from tests.funnel_registry_fixtures import write_registry


FUNNEL_ID = "mfm_business_ai_001"
CHANNEL_ID = "mfm_business_ai_primary"


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
            "source_type": "youtube_channels",
            "sources": [
                {
                    "source_id": "my_first_million",
                    "label": "My First Million",
                    "url": "https://www.youtube.com/@MyFirstMillionPod/videos",
                    "source_type": "youtube_channel",
                    "active": True,
                    "max_videos_per_source": 25,
                    "hydrate_missing_duration": True,
                    "title_allowlist": ["MFM"],
                    "title_blocklist": ["shorts"],
                }
            ],
            "min_duration_minutes": 25,
            "max_duration_minutes": 180,
            "max_downloads_per_run": 1,
        },
        "processing": {
            "pipeline_profile": FUNNEL_ID,
            "ai_rules": {"ai_rule_profile": "business", "prompt_managed": "builtin"},
            "selection": {
                "max_clips": 6,
                "min_clip_duration_sec": 15,
                "max_clip_duration_sec": 60,
                "max_overlap_sec": 2,
            },
            "output": {
                "filename_prefix": "mfm_business_ai",
                "delivery_mode": "pull_from_output_endpoint",
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
            "posting_enabled": True,
            "posting_mode": "manual_review",
            "target_platforms": ["youtube_shorts"],
            "channel_routes": [
                {
                    "channel_id": CHANNEL_ID,
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
    environment: str = "dev",
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
        operator_password="secret-pass",
        secret_key="test-secret-key",
        environment=environment,
        services=(),
    )


def _source_fixture() -> list[dict]:
    return [
        {
            "funnel_id": "template_youtube_podcast_001",
            "angle": "template",
            "source_type": "youtube_channels",
            "sources": [],
            "min_duration_minutes": 20,
            "max_duration_minutes": 180,
            "max_downloads_per_run": 1,
            "active": False,
            "posting_config": {"enabled": False, "mode": "manual_review"},
            "analytics_config": {"enabled": False, "event_namespace": "template"},
        }
    ]


def _channels_fixture(*, accepted: list[str] | None = None) -> dict:
    return {
        "channels": [
            {
                "channel_id": CHANNEL_ID,
                "brand_name": "MFM Business AI",
                "platform": "youtube_shorts",
                "enabled": True,
                "priority": 10,
                "credentials": {"token_file_env": "MFM_BUSINESS_AI_YT_TOKEN_FILE"},
                "routing": {
                    "accepted_funnel_ids": accepted or [],
                    "min_composite_score": 0,
                    "required_platform": "youtube_shorts",
                },
                "cadence": {"timezone": "UTC", "min_gap_minutes": 120},
                "metadata_style": {"default_hashtags": ["#Shorts"]},
            }
        ]
    }


def _configure_sync_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, env_name: str = "dev") -> dict[str, Path]:
    root = tmp_path / "etc" / env_name
    source_dir = root / "source-input"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "funnels.json"
    source_path.write_text(json.dumps(_source_fixture()), encoding="utf-8")
    video_dir = root / "video-automation" / "funnels"
    video_dir.mkdir(parents=True)
    channels_path = root / "output-funnel" / "channels.json"
    channels_path.parent.mkdir(parents=True)
    channels_path.write_text(json.dumps(_channels_fixture()), encoding="utf-8")
    ai_registry = tmp_path / "funnel_rule_registry.json"
    write_registry(ai_registry)
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "business_v1.txt").write_text("rules", encoding="utf-8")
    config_dir = tmp_path / "config_funnels"
    config_dir.mkdir()
    (config_dir / "business.yaml").write_text("funnel:\n  id: business\n", encoding="utf-8")

    monkeypatch.setenv("MK04_CONFIG_ROOT", str(root))
    monkeypatch.setenv("MK04_ENV", env_name)
    monkeypatch.setenv("INPUT_SERVICE_CONFIG_DIR", str(source_dir))
    monkeypatch.setenv("FUNNEL_CONFIG_DIR", str(video_dir))
    monkeypatch.setenv("OUTPUT_FUNNEL_CHANNELS", str(channels_path))
    monkeypatch.setenv("AI_FUNNEL_RULE_REGISTRY", str(ai_registry))
    monkeypatch.setenv("AI_FUNNEL_RULES_DIR", str(prompts_dir))
    monkeypatch.setenv("CONFIG_MANAGER_FUNNELS_DIR", str(config_dir))

    return {
        "root": root,
        "source": source_path,
        "video_dir": video_dir,
        "channels": channels_path,
        "ai_registry": ai_registry,
        "prompts": prompts_dir,
        "config_dir": config_dir,
    }


def _save_funnel(registry_dir: Path, **identity_overrides: object) -> None:
    funnel = load_canonical_funnel(_valid_funnel_payload(**identity_overrides))
    FunnelRegistry(registry_dir).save_funnel(funnel)


@pytest.fixture
def registry_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir()
    monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))
    _save_funnel(registry_dir)
    return registry_dir


@pytest.fixture
def sync_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    return _configure_sync_paths(tmp_path, monkeypatch, env_name="dev")


def _login(client, password: str = "secret-pass", *, next_url: str | None = None) -> None:
    page = client.get("/login")
    html = page.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    token = html.split(marker, 1)[1].split('"', 1)[0]
    client.post(
        "/login",
        data={
            "password": password,
            "csrf_token": token,
            "next": next_url or f"/funnels/{FUNNEL_ID}/sync",
        },
    )


def _csrf_token(client, funnel_id: str = FUNNEL_ID, *, auth_enabled: bool = False) -> str:
    page = client.get(f"/funnels/{funnel_id}/sync")
    html = page.get_data(as_text=True)
    if not auth_enabled:
        return ""
    marker = 'name="csrf_token" value="'
    return html.split(marker, 1)[1].split('"', 1)[0]


def _sync_post(
    client,
    *,
    environment: str = "dev",
    prod_confirm: str = "",
    include_confirm: bool = True,
    csrf: bool = True,
    auth_enabled: bool = False,
) -> dict[str, str]:
    data: dict[str, str] = {
        "environment": environment,
    }
    if include_confirm:
        data["confirm_understand"] = "on"
    if prod_confirm:
        data["prod_confirm"] = prod_confirm
    if csrf and auth_enabled:
        data["csrf_token"] = _csrf_token(client, auth_enabled=True)
    return data


class TestGetPreview:
    def test_get_renders_for_saved_funnel(self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]) -> None:
        response = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}/sync")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        assert "Sync Config" in body
        assert FUNNEL_ID in body
        assert "MFM Business AI" in body
        assert str(sync_env["source"]) in body
        assert "create" in body
        assert "AI registry" in body or "registry" in body.lower()

    def test_sync_preview_shows_processing_readiness(self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]) -> None:
        body = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}/sync").data.decode("utf-8")
        assert "Processing now" in body
        assert "After sync" in body

    def test_posting_disabled_with_missing_channel_still_allows_apply(
        self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]
    ) -> None:
        payload = _valid_funnel_payload()
        payload["distribution"]["posting_enabled"] = False
        payload["distribution"]["posting_mode"] = "disabled"
        payload["distribution"]["target_platforms"] = []
        payload["distribution"]["channel_routes"] = [
            {"channel_id": "missing_channel", "platform": "youtube_shorts", "enabled": True}
        ]
        funnel = load_canonical_funnel(payload)
        FunnelRegistry(registry_env).save_funnel(funnel, overwrite=True)
        body = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}/sync").data.decode("utf-8")
        assert "ready to apply" in body
        assert "Sync runtime config" in body
        assert "blocked" not in body.split("Sync preview")[1].split("Advanced sync details")[0].lower()

    def test_missing_funnel_returns_404(self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]) -> None:
        response = create_app(_settings(tmp_path)).test_client().get("/funnels/missing/sync")
        assert response.status_code == 404

    def test_get_does_not_write_files(self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]) -> None:
        before_source = sync_env["source"].read_text(encoding="utf-8")
        before_channels = sync_env["channels"].read_text(encoding="utf-8")
        create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}/sync")
        assert sync_env["source"].read_text(encoding="utf-8") == before_source
        assert sync_env["channels"].read_text(encoding="utf-8") == before_channels
        assert not list(sync_env["video_dir"].glob("*.json"))

    def test_warnings_and_errors_render(self, tmp_path: Path, registry_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MK04_CONFIG_ROOT", raising=False)
        monkeypatch.delenv("INPUT_SERVICE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("FUNNEL_CONFIG_DIR", raising=False)
        monkeypatch.delenv("OUTPUT_FUNNEL_CHANNELS", raising=False)
        body = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}/sync?environment=prod").data.decode("utf-8")
        assert "PRODUCTION" in body
        assert "high risk" in body.lower()
        assert "not resolved" in body.lower() or "not configured" in body.lower()


class TestEnvironmentHandling:
    def test_dev_paths_resolve(self, tmp_path: Path, sync_env: dict[str, Path]) -> None:
        paths = resolve_sync_paths("dev")
        assert paths.source_funnels_path == sync_env["source"]
        assert paths.video_funnels_dir == sync_env["video_dir"]
        assert paths.output_channels_path == sync_env["channels"]

    def test_prod_preview_marked_high_risk(self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]) -> None:
        body = (
            create_app(_settings(tmp_path))
            .test_client()
            .get(f"/funnels/{FUNNEL_ID}/sync?environment=prod")
            .data.decode("utf-8")
        )
        assert "PRODUCTION" in body
        assert "high risk" in body.lower()

    def test_invalid_environment_rejected(self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]) -> None:
        body = (
            create_app(_settings(tmp_path))
            .test_client()
            .get(f"/funnels/{FUNNEL_ID}/sync?environment=staging")
            .data.decode("utf-8")
        )
        assert "Invalid sync environment" in body

    def test_prod_without_config_root_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MK04_CONFIG_ROOT", raising=False)
        monkeypatch.delenv("INPUT_SERVICE_CONFIG_DIR", raising=False)
        paths = resolve_sync_paths("prod")
        assert paths.source_funnels_path is None
        assert any("Production config root" in warning for warning in paths.warnings)


class TestPostApply:
    def test_valid_post_applies_sync(self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        response = client.post(f"/funnels/{FUNNEL_ID}/sync", data=_sync_post(client))
        assert response.status_code == 302
        assert response.location.endswith(f"/funnels/{FUNNEL_ID}")
        assert (sync_env["video_dir"] / f"{FUNNEL_ID}.json").is_file()
        channels = json.loads(sync_env["channels"].read_text(encoding="utf-8"))
        accepted = channels["channels"][0]["routing"]["accepted_funnel_ids"]
        assert FUNNEL_ID in accepted

    def test_success_flash_lists_changes(self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        response = client.post(
            f"/funnels/{FUNNEL_ID}/sync",
            data=_sync_post(client),
            follow_redirects=True,
        )
        assert b"Synced successfully" in response.data

    def test_post_with_blocking_errors_does_not_write(
        self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]
    ) -> None:
        sync_env["source"].write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        before = sync_env["channels"].read_text(encoding="utf-8")
        client = create_app(_settings(tmp_path)).test_client()
        response = client.post(f"/funnels/{FUNNEL_ID}/sync", data=_sync_post(client))
        assert response.status_code == 200
        assert sync_env["channels"].read_text(encoding="utf-8") == before
        assert not (sync_env["video_dir"] / f"{FUNNEL_ID}.json").exists()

    def test_post_without_confirmation_does_not_write(
        self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]
    ) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        response = client.post(
            f"/funnels/{FUNNEL_ID}/sync",
            data=_sync_post(client, include_confirm=False),
        )
        assert response.status_code == 200
        assert b"runtime config files will be written" in response.data
        assert not (sync_env["video_dir"] / f"{FUNNEL_ID}.json").exists()

    def test_csrf_required_when_auth_enabled(
        self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]
    ) -> None:
        client = create_app(_settings(tmp_path, auth_enabled=True)).test_client()
        _login(client)
        response = client.post(
            f"/funnels/{FUNNEL_ID}/sync",
            data=_sync_post(client, csrf=False, auth_enabled=True),
        )
        assert response.status_code == 200
        assert b"Invalid security token" in response.data
        assert not (sync_env["video_dir"] / f"{FUNNEL_ID}.json").exists()


class TestProdSafeguards:
    def test_prod_post_without_prod_confirmation_rejected(
        self, tmp_path: Path, registry_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        targets = _configure_sync_paths(tmp_path, monkeypatch, env_name="prod")
        client = create_app(_settings(tmp_path, environment="prod")).test_client()
        response = client.post(
            f"/funnels/{FUNNEL_ID}/sync",
            data=_sync_post(client, environment="prod"),
        )
        assert response.status_code == 200
        assert b"funnel ID" in response.data
        assert not (targets["video_dir"] / f"{FUNNEL_ID}.json").exists()

    def test_prod_post_with_confirmation_applies(
        self, tmp_path: Path, registry_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        targets = _configure_sync_paths(tmp_path, monkeypatch, env_name="prod")
        client = create_app(_settings(tmp_path, environment="prod")).test_client()
        response = client.post(
            f"/funnels/{FUNNEL_ID}/sync",
            data=_sync_post(client, environment="prod", prod_confirm=FUNNEL_ID),
        )
        assert response.status_code == 302
        assert (targets["video_dir"] / f"{FUNNEL_ID}.json").is_file()

    def test_prod_apply_uses_backup(
        self, tmp_path: Path, registry_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        targets = _configure_sync_paths(tmp_path, monkeypatch, env_name="prod")
        client = create_app(_settings(tmp_path, environment="prod")).test_client()
        client.post(
            f"/funnels/{FUNNEL_ID}/sync",
            data=_sync_post(client, environment="prod", prod_confirm=FUNNEL_ID),
        )
        backups = list(targets["source"].parent.glob("funnels.json.bak.*"))
        assert backups

    def test_prod_paths_not_hardcoded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MK04_CONFIG_ROOT", raising=False)
        monkeypatch.delenv("INPUT_SERVICE_CONFIG_DIR", raising=False)
        paths = resolve_sync_paths("prod")
        for value in (
            paths.source_funnels_path,
            paths.video_funnels_dir,
            paths.output_channels_path,
        ):
            if value is not None:
                assert "/etc/mk04/prod" not in str(value)


class TestScopeProtection:
    def test_builtin_prompt_not_overwritten_on_apply(
        self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]
    ) -> None:
        prompt_before = (sync_env["prompts"] / "business_v1.txt").read_text(encoding="utf-8")
        client = create_app(_settings(tmp_path)).test_client()
        client.post(f"/funnels/{FUNNEL_ID}/sync", data=_sync_post(client))
        assert (sync_env["prompts"] / "business_v1.txt").read_text(encoding="utf-8") == prompt_before

    def test_config_manager_yaml_may_be_updated_on_apply(
        self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]
    ) -> None:
        client = create_app(_settings(tmp_path)).test_client()
        client.post(f"/funnels/{FUNNEL_ID}/sync", data=_sync_post(client))
        yaml_text = (sync_env["config_dir"] / "business.yaml").read_text(encoding="utf-8")
        assert "MFM Business AI" in yaml_text

    def test_no_pipeline_or_profile_writes(
        self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]
    ) -> None:
        pipeline = tmp_path / "pipeline_config.json"
        profiles = tmp_path / "video_pipeline_profiles.json"
        pipeline.write_text("{}", encoding="utf-8")
        profiles.write_text("{}", encoding="utf-8")
        before_pipeline = pipeline.read_text(encoding="utf-8")
        before_profiles = profiles.read_text(encoding="utf-8")
        client = create_app(_settings(tmp_path)).test_client()
        client.post(f"/funnels/{FUNNEL_ID}/sync", data=_sync_post(client))
        assert pipeline.read_text(encoding="utf-8") == before_pipeline
        assert profiles.read_text(encoding="utf-8") == before_profiles

    def test_channels_credentials_and_cadence_unchanged(
        self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]
    ) -> None:
        before = json.loads(sync_env["channels"].read_text(encoding="utf-8"))
        client = create_app(_settings(tmp_path)).test_client()
        client.post(f"/funnels/{FUNNEL_ID}/sync", data=_sync_post(client))
        after = json.loads(sync_env["channels"].read_text(encoding="utf-8"))
        channel_before = before["channels"][0]
        channel_after = after["channels"][0]
        assert channel_before["credentials"] == channel_after["credentials"]
        assert channel_before["cadence"] == channel_after["cadence"]
        assert channel_before["metadata_style"] == channel_after["metadata_style"]


class TestUiIntegration:
    def test_detail_page_shows_sync_link(self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]) -> None:
        body = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}").data.decode("utf-8")
        assert "Sync runtime config" in body
        assert f"/funnels/{FUNNEL_ID}/sync" in body

    def test_sync_page_has_back_link(self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]) -> None:
        body = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}/sync").data.decode("utf-8")
        assert f"/funnels/{FUNNEL_ID}" in body

    def test_confirmation_form_includes_csrf(self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]) -> None:
        body = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}/sync").data
        assert b'name="csrf_token"' in body
        assert b"confirm_understand" in body

    def test_sync_page_does_not_show_credential_values(
        self, tmp_path: Path, registry_env: Path, sync_env: dict[str, Path]
    ) -> None:
        body = create_app(_settings(tmp_path)).test_client().get(f"/funnels/{FUNNEL_ID}/sync").data.decode("utf-8")
        assert "MFM_BUSINESS_AI_YT_TOKEN_FILE" not in body
