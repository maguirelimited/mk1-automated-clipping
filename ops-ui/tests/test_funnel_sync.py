"""Tests for the configuration synchronisation layer (Funnel Management MK1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ops_ui.funnel_management.schema import load_canonical_funnel
from ops_ui.funnel_management.sync import (
    FunnelSyncError,
    FunnelSyncTargetPaths,
    FunnelSynchronizer,
)
from tests.funnel_registry_fixtures import registry_document, write_registry


FUNNEL_ID = "mfm_business_ai_001"
OTHER_ID = "template_youtube_podcast_001"
CHANNEL_ID = "mfm_business_ai_primary"
CUSTOM_FUNNEL_ID = "gaming_clips_001"


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
        "mappings": {
            "config_manager_funnel_id": "business",
            "config_manager_preset_id": "balanced",
        },
    }


def _custom_funnel_payload() -> dict:
    payload = _valid_funnel_payload(
        funnel_id=CUSTOM_FUNNEL_ID,
        display_name="Gaming Clips",
    )
    payload["processing"]["pipeline_profile"] = CUSTOM_FUNNEL_ID
    payload["processing"]["ai_rules"] = {
        "ai_rule_profile": "gaming",
        "prompt_managed": "custom",
        "prompt_text": "Select high-energy gaming highlight clips.",
    }
    payload["distribution"]["channel_routes"] = []
    payload["distribution"]["posting_enabled"] = False
    payload["distribution"]["target_platforms"] = []
    payload["mappings"] = {"config_manager_preset_id": "growth", "config_manager_funnel_id": None}
    return payload


def _funnel(**identity_overrides: object) -> object:
    return load_canonical_funnel(_valid_funnel_payload(**identity_overrides))


def _source_fixture() -> list[dict]:
    return [
        {
            "funnel_id": OTHER_ID,
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


@pytest.fixture
def sync_env(tmp_path: Path) -> dict[str, Path]:
    source_path = tmp_path / "funnels.json"
    source_path.write_text(json.dumps(_source_fixture()), encoding="utf-8")
    video_dir = tmp_path / "video_funnels"
    video_dir.mkdir()
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(json.dumps(_channels_fixture()), encoding="utf-8")
    ai_registry = tmp_path / "funnel_rule_registry.json"
    write_registry(ai_registry)
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "business_v1.txt").write_text("builtin rules", encoding="utf-8")
    config_dir = tmp_path / "config_funnels"
    config_dir.mkdir()
    (config_dir / "business.yaml").write_text(
        "funnel:\n  id: business\n  name: Legacy Business\n  preset: growth\n  enabled: true\n"
        "custom_section:\n  keep_me: true\n",
        encoding="utf-8",
    )
    return {
        "source": source_path,
        "video_dir": video_dir,
        "channels": channels_path,
        "ai_registry": ai_registry,
        "prompts": prompts_dir,
        "config_dir": config_dir,
    }


def _paths(env: dict[str, Path], **overrides: Path | None) -> FunnelSyncTargetPaths:
    values = {
        "source_funnels_path": env["source"],
        "video_funnels_dir": env["video_dir"],
        "output_channels_path": env["channels"],
        "ai_rule_registry_path": env["ai_registry"],
        "ai_prompts_dir": env["prompts"],
        "config_manager_funnels_dir": env["config_dir"],
    }
    values.update(overrides)
    return FunnelSyncTargetPaths(**values)


def _change(report, target: str):
    return next(item for item in report.changes if item.target == target)


class TestDryRunPlan:
    def test_build_plan_does_not_write(self, sync_env: dict[str, Path]) -> None:
        before_source = sync_env["source"].read_text(encoding="utf-8")
        before_channels = sync_env["channels"].read_text(encoding="utf-8")
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(_funnel())
        assert report.dry_run is True
        assert sync_env["source"].read_text(encoding="utf-8") == before_source
        assert sync_env["channels"].read_text(encoding="utf-8") == before_channels
        assert not list(sync_env["video_dir"].glob("*.json"))

    def test_plan_reports_create_and_update(self, sync_env: dict[str, Path]) -> None:
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(_funnel())
        assert report.ok is True
        assert report.changed is True
        assert _change(report, "source_input_funnels").action == "create"
        assert _change(report, "video_funnel_json").action == "create"
        assert _change(report, "output_channels").action == "update"

    def test_missing_write_target_is_blocking(self, sync_env: dict[str, Path]) -> None:
        report = FunnelSynchronizer(
            _paths(sync_env, source_funnels_path=None)
        ).build_plan(_funnel())
        assert report.ok is False
        assert any("not configured" in error for error in report.errors)

    def test_optional_validate_paths_warn_when_missing(self, sync_env: dict[str, Path]) -> None:
        report = FunnelSynchronizer(
            _paths(sync_env, ai_rule_registry_path=None, config_manager_funnels_dir=None)
        ).build_plan(_funnel())
        assert any("AI registry was not updated" in warning for warning in report.warnings)
        assert any("ConfigManager YAML was not written" in warning for warning in report.warnings)


class TestSourceInputProjection:
    def test_creates_and_preserves_template(self, sync_env: dict[str, Path]) -> None:
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(_funnel())
        after = _change(report, "source_input_funnels").after
        assert isinstance(after, list)
        assert len(after) == 2
        template = next(item for item in after if item["funnel_id"] == OTHER_ID)
        assert template["posting_config"]["enabled"] is False
        assert template["analytics_config"]["event_namespace"] == "template"

    def test_updates_existing_entry(self, sync_env: dict[str, Path]) -> None:
        existing = _source_fixture()
        existing.append(
            {
                "funnel_id": FUNNEL_ID,
                "angle": "old angle",
                "source_type": "youtube_channels",
                "sources": [],
                "min_duration_minutes": 10,
                "max_duration_minutes": 60,
                "max_downloads_per_run": 1,
                "active": False,
                "posting_config": {"enabled": True, "mode": "auto_queue"},
                "analytics_config": {"enabled": True, "event_namespace": "old"},
            }
        )
        sync_env["source"].write_text(json.dumps(existing), encoding="utf-8")
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(_funnel())
        entry = next(item for item in _change(report, "source_input_funnels").after if item["funnel_id"] == FUNNEL_ID)
        assert entry["active"] is True
        assert entry["angle"] == "Business podcast clipping funnel"
        assert entry["posting_config"]["enabled"] is True
        assert entry["analytics_config"]["event_namespace"] == "old"

    def test_rejects_max_downloads_not_one(self, sync_env: dict[str, Path]) -> None:
        payload = _valid_funnel_payload()
        payload["acquisition"]["max_downloads_per_run"] = 2
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(load_canonical_funnel(payload))
        assert report.ok is False
        assert any("max_downloads_per_run must be 1" in error for error in report.errors)

    def test_playlist_canonical_projects_to_runtime_shape(self, sync_env: dict[str, Path]) -> None:
        payload = _valid_funnel_payload(funnel_id="youtube_playlist_test_001")
        payload["acquisition"]["source_type"] = "youtube_playlist"
        payload["acquisition"]["sources"] = [
            {
                "source_id": "curated_clips",
                "label": "Curated Clips",
                "url": "https://www.youtube.com/playlist?list=PLtest123",
                "source_type": "youtube_playlist",
                "active": True,
                "max_videos_per_source": 10,
                "hydrate_missing_duration": True,
                "title_allowlist": [],
                "title_blocklist": [],
            }
        ]
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(load_canonical_funnel(payload))
        entry = next(
            item
            for item in _change(report, "source_input_funnels").after
            if item["funnel_id"] == "youtube_playlist_test_001"
        )
        assert entry["source_type"] == "youtube_playlists"
        assert entry["sources"][0]["source_type"] == "youtube_playlist"
        assert entry["sources"][0]["url"].startswith("https://www.youtube.com/playlist")


class TestVideoProjection:
    def test_creates_video_file(self, sync_env: dict[str, Path]) -> None:
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(_funnel())
        video = _change(report, "video_funnel_json").after
        assert video["selection"]["min_duration_sec"] == 15
        assert video["output"]["delivery_mode"] == "pull_from_output_endpoint"

    def test_handoff_maps_with_warning(self, sync_env: dict[str, Path]) -> None:
        payload = _valid_funnel_payload()
        payload["processing"]["output"]["delivery_mode"] = "handoff"
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(load_canonical_funnel(payload))
        video = _change(report, "video_funnel_json").after
        assert video["output"]["delivery_mode"] == "pull_from_output_endpoint"
        assert any("handoff" in warning for warning in report.warnings)

    def test_facebook_reels_warning(self, sync_env: dict[str, Path]) -> None:
        payload = _valid_funnel_payload()
        payload["processing"]["platforms"]["facebook_reels"] = True
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(load_canonical_funnel(payload))
        video = _change(report, "video_funnel_json").after
        assert "facebook_reels" not in video["platforms"]
        assert any("facebook_reels" in warning for warning in report.warnings)

    def test_rejects_mismatched_existing_funnel_id(self, sync_env: dict[str, Path]) -> None:
        path = sync_env["video_dir"] / f"{FUNNEL_ID}.json"
        path.write_text(json.dumps({"funnel_id": "other_id", "funnel_name": "X"}), encoding="utf-8")
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(_funnel())
        assert report.ok is False
        assert any("mismatched funnel_id" in error for error in report.errors)


class TestOutputChannelsPatch:
    def test_adds_accepted_funnel_id(self, sync_env: dict[str, Path]) -> None:
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(_funnel())
        after = _change(report, "output_channels").after
        accepted = after["channels"][0]["routing"]["accepted_funnel_ids"]
        assert FUNNEL_ID in accepted
        assert after["channels"][0]["credentials"]["token_file_env"] == "MFM_BUSINESS_AI_YT_TOKEN_FILE"

    def test_missing_channel_is_blocking(self, sync_env: dict[str, Path]) -> None:
        payload = _valid_funnel_payload()
        payload["distribution"]["channel_routes"] = [
            {"channel_id": "missing_channel", "platform": "youtube_shorts", "enabled": True}
        ]
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(load_canonical_funnel(payload))
        assert report.ok is False
        assert any("missing_channel" in error for error in report.errors)

    def test_missing_channel_is_non_blocking_when_posting_disabled(self, sync_env: dict[str, Path]) -> None:
        payload = _valid_funnel_payload()
        payload["distribution"]["posting_enabled"] = False
        payload["distribution"]["posting_mode"] = "disabled"
        payload["distribution"]["target_platforms"] = []
        payload["distribution"]["channel_routes"] = [
            {"channel_id": "missing_channel", "platform": "youtube_shorts", "enabled": True}
        ]
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(load_canonical_funnel(payload))
        assert report.ok is True
        assert any("posting disabled" in warning.lower() for warning in report.warnings)
        assert _change(report, "output_channels").action == "skipped"

    def test_platform_mismatch_is_blocking(self, sync_env: dict[str, Path]) -> None:
        payload = _valid_funnel_payload()
        payload["distribution"]["channel_routes"] = [
            {"channel_id": CHANNEL_ID, "platform": "tiktok", "enabled": True}
        ]
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(load_canonical_funnel(payload))
        assert report.ok is False
        assert any("platform mismatch" in error for error in report.errors)

    def test_disabled_route_does_not_add(self, sync_env: dict[str, Path]) -> None:
        payload = _valid_funnel_payload()
        payload["distribution"]["channel_routes"] = [
            {"channel_id": CHANNEL_ID, "platform": "youtube_shorts", "enabled": False}
        ]
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(load_canonical_funnel(payload))
        after = _change(report, "output_channels").after
        assert FUNNEL_ID not in after["channels"][0]["routing"]["accepted_funnel_ids"]
        assert _change(report, "output_channels").changed is False


class TestBuiltinAiRegistrySync:
    def test_builtin_alias_unchanged_when_correct(self, sync_env: dict[str, Path]) -> None:
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(_funnel())
        registry_change = _change(report, "funnel_rule_registry")
        assert registry_change.action == "unchanged"
        assert _change(report, "ai_prompt_file").action == "skipped"

    def test_builtin_missing_alias_patches_registry(self, sync_env: dict[str, Path]) -> None:
        write_registry(sync_env["ai_registry"], registry_document(include_funnel_alias=False))
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(_funnel())
        registry_change = _change(report, "funnel_rule_registry")
        assert registry_change.action == "update"
        assert registry_change.after["aliases"][FUNNEL_ID] == "business"

    def test_builtin_missing_prompt_blocks(self, sync_env: dict[str, Path]) -> None:
        (sync_env["prompts"] / "business_v1.txt").unlink()
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(_funnel())
        assert report.ok is False
        assert any("AI prompt file missing" in error for error in report.errors)

    def test_builtin_does_not_write_prompt_file(self, sync_env: dict[str, Path]) -> None:
        prompt_before = (sync_env["prompts"] / "business_v1.txt").read_text(encoding="utf-8")
        FunnelSynchronizer(_paths(sync_env)).apply(_funnel())
        assert (sync_env["prompts"] / "business_v1.txt").read_text(encoding="utf-8") == prompt_before


class TestCustomAiRegistrySync:
    def test_custom_dry_run_shows_profile_alias_and_prompt(self, sync_env: dict[str, Path]) -> None:
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(load_canonical_funnel(_custom_funnel_payload()))
        assert _change(report, "funnel_rule_registry").action in {"create", "update"}
        assert _change(report, "ai_prompt_file").action == "create"
        assert _change(report, "config_manager_yaml").action == "create"

    def test_apply_writes_custom_profile_alias_and_prompt(self, sync_env: dict[str, Path]) -> None:
        funnel = load_canonical_funnel(_custom_funnel_payload())
        FunnelSynchronizer(_paths(sync_env)).apply(funnel)
        registry = json.loads(sync_env["ai_registry"].read_text(encoding="utf-8"))
        assert registry["profiles"]["gaming"]["rules_version"] == "gaming_v1"
        assert registry["profiles"]["gaming"]["managed"] == "ops_ui"
        assert registry["aliases"][CUSTOM_FUNNEL_ID] == "gaming"
        prompt_path = sync_env["prompts"] / "gaming_v1.txt"
        assert prompt_path.is_file()
        assert "gaming highlight" in prompt_path.read_text(encoding="utf-8")

    def test_custom_sync_is_idempotent(self, sync_env: dict[str, Path]) -> None:
        funnel = load_canonical_funnel(_custom_funnel_payload())
        synchronizer = FunnelSynchronizer(_paths(sync_env))
        synchronizer.apply(funnel)
        second = synchronizer.build_plan(funnel)
        assert _change(second, "funnel_rule_registry").action == "unchanged"
        assert _change(second, "ai_prompt_file").action == "unchanged"

    def test_refuses_overwrite_builtin_profile_as_custom(self, sync_env: dict[str, Path]) -> None:
        payload = _custom_funnel_payload()
        payload["processing"]["ai_rules"]["ai_rule_profile"] = "business"
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(load_canonical_funnel(payload))
        assert report.ok is False
        assert any("built-in AI profile" in error for error in report.errors)


class TestConfigManagerYamlSync:
    def test_dry_run_creates_missing_yaml(self, sync_env: dict[str, Path]) -> None:
        (sync_env["config_dir"] / "business.yaml").unlink()
        report = FunnelSynchronizer(_paths(sync_env)).build_plan(_funnel())
        yaml_change = _change(report, "config_manager_yaml")
        assert yaml_change.action == "create"
        assert "youtube" in yaml_change.after_text

    def test_apply_creates_yaml_with_owned_fields(self, sync_env: dict[str, Path]) -> None:
        (sync_env["config_dir"] / "business.yaml").unlink()
        FunnelSynchronizer(_paths(sync_env)).apply(_funnel())
        yaml_path = sync_env["config_dir"] / "business.yaml"
        assert yaml_path.is_file()
        doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert doc["funnel"]["id"] == "business"
        assert doc["funnel"]["name"] == "MFM Business AI"
        assert doc["funnel"]["preset"] == "balanced"
        assert doc["funnel"]["enabled"] is True
        assert doc["platforms"]["enabled"] == ["youtube"]

    def test_existing_yaml_preserves_unknown_keys(self, sync_env: dict[str, Path]) -> None:
        FunnelSynchronizer(_paths(sync_env)).apply(_funnel())
        doc = yaml.safe_load((sync_env["config_dir"] / "business.yaml").read_text(encoding="utf-8"))
        assert doc["custom_section"]["keep_me"] is True
        assert doc["funnel"]["name"] == "MFM Business AI"

    def test_missing_preset_falls_back_to_balanced(self, sync_env: dict[str, Path]) -> None:
        payload = _valid_funnel_payload()
        payload["mappings"].pop("config_manager_preset_id", None)
        FunnelSynchronizer(_paths(sync_env)).apply(load_canonical_funnel(payload))
        doc = yaml.safe_load((sync_env["config_dir"] / "business.yaml").read_text(encoding="utf-8"))
        assert doc["funnel"]["preset"] == "balanced"


class TestApply:
    def test_apply_writes_atomically(self, sync_env: dict[str, Path]) -> None:
        report = FunnelSynchronizer(_paths(sync_env)).apply(_funnel())
        assert report.ok is True
        assert (sync_env["video_dir"] / f"{FUNNEL_ID}.json").is_file()
        source = json.loads(sync_env["source"].read_text(encoding="utf-8"))
        assert any(item["funnel_id"] == FUNNEL_ID for item in source)
        channels = json.loads(sync_env["channels"].read_text(encoding="utf-8"))
        assert FUNNEL_ID in channels["channels"][0]["routing"]["accepted_funnel_ids"]

    def test_apply_refuses_on_blocking_errors(self, sync_env: dict[str, Path]) -> None:
        payload = _valid_funnel_payload()
        payload["acquisition"]["max_downloads_per_run"] = 3
        before = sync_env["source"].read_text(encoding="utf-8")
        with pytest.raises(FunnelSyncError):
            FunnelSynchronizer(_paths(sync_env)).apply(load_canonical_funnel(payload))
        assert sync_env["source"].read_text(encoding="utf-8") == before

    def test_backup_created_when_requested(self, sync_env: dict[str, Path]) -> None:
        existing = _source_fixture()
        existing.append(
            {
                "funnel_id": FUNNEL_ID,
                "angle": "old",
                "source_type": "youtube_channels",
                "sources": [],
                "min_duration_minutes": 1,
                "max_duration_minutes": 2,
                "max_downloads_per_run": 1,
                "active": False,
            }
        )
        sync_env["source"].write_text(json.dumps(existing), encoding="utf-8")
        FunnelSynchronizer(_paths(sync_env)).apply(_funnel(), backup=True)
        backups = list(sync_env["source"].parent.glob("funnels.json.bak.*"))
        assert backups


class TestIntegrationPath:
    def test_custom_funnel_full_projection(self, sync_env: dict[str, Path]) -> None:
        funnel = load_canonical_funnel(_custom_funnel_payload())
        report = FunnelSynchronizer(_paths(sync_env)).apply(funnel)
        assert report.ok is True
        assert (sync_env["video_dir"] / f"{CUSTOM_FUNNEL_ID}.json").is_file()
        registry = json.loads(sync_env["ai_registry"].read_text(encoding="utf-8"))
        assert registry["aliases"][CUSTOM_FUNNEL_ID] == "gaming"
        assert (sync_env["prompts"] / "gaming_v1.txt").is_file()
        assert (sync_env["config_dir"] / f"{CUSTOM_FUNNEL_ID}.yaml").is_file()


class TestScopeProtection:
    def test_no_pipeline_or_profile_files(self, sync_env: dict[str, Path]) -> None:
        pipeline = sync_env["source"].parent / "pipeline_config.json"
        profiles = sync_env["source"].parent / "video_pipeline_profiles.json"
        pipeline.write_text("{}", encoding="utf-8")
        profiles.write_text("{}", encoding="utf-8")
        FunnelSynchronizer(_paths(sync_env)).apply(_funnel())
        assert pipeline.read_text(encoding="utf-8") == "{}"
        assert profiles.read_text(encoding="utf-8") == "{}"
