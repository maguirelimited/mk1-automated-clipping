"""Unit tests for the funnel validation engine (Funnel Management MK1)."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from ops_ui.funnel_management.schema import dump_canonical_funnel, load_canonical_funnel
from ops_ui.funnel_management.validation import (
    FunnelValidationSeverity,
    FunnelValidator,
)
from tests.funnel_registry_fixtures import registry_document, write_registry


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
                    "title_allowlist": [],
                    "title_blocklist": ["shorts"],
                }
            ],
            "min_duration_minutes": 25,
            "max_duration_minutes": 180,
            "max_downloads_per_run": 1,
        },
        "processing": {
            "pipeline_profile": FUNNEL_ID,
            "ai_rules": {"ai_rule_profile": "business"},
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
                    "channel_id": "mfm_business_ai_primary",
                    "platform": "youtube_shorts",
                    "enabled": True,
                }
            ],
        },
        "mappings": {"config_manager_funnel_id": "business"},
    }


def _write_dependency_fixtures(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)

    source_path = root / "funnels.json"
    source_path.write_text(
        json.dumps(
            [
                {
                    "funnel_id": FUNNEL_ID,
                    "angle": "business productivity ai leverage podcasts",
                    "source_type": "youtube_channels",
                    "pipeline_profile": FUNNEL_ID,
                    "sources": [
                        {
                            "source_id": "my_first_million",
                            "label": "My First Million",
                            "source_type": "youtube_channel",
                            "url": "https://www.youtube.com/@MyFirstMillionPod/videos",
                            "active": True,
                            "max_videos_per_source": 25,
                        }
                    ],
                    "min_duration_minutes": 25,
                    "max_duration_minutes": 180,
                    "max_downloads_per_run": 1,
                    "active": True,
                }
            ]
        ),
        encoding="utf-8",
    )

    video_dir = root / "video_funnels"
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / f"{FUNNEL_ID}.json").write_text(
        json.dumps(
            {
                "funnel_id": FUNNEL_ID,
                "funnel_name": "MFM Business AI",
                "platforms": {"youtube_shorts": True, "tiktok": False, "instagram_reels": False, "x": False},
                "selection": {
                    "max_clips": 6,
                    "min_duration_sec": 15,
                    "max_duration_sec": 60,
                    "max_overlap_sec": 2,
                },
                "output": {
                    "filename_prefix": "mfm_business_ai",
                    "delivery_mode": "pull_from_output_endpoint",
                },
            }
        ),
        encoding="utf-8",
    )

    profiles_path = root / "video_pipeline_profiles.json"
    profiles_path.write_text(
        json.dumps({"profiles": {FUNNEL_ID: {"selection": {"max_clips": 3}}}}),
        encoding="utf-8",
    )

    channels_path = root / "channels.json"
    channels_path.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "channel_id": "mfm_business_ai_primary",
                        "platform": "youtube_shorts",
                        "enabled": True,
                        "routing": {"accepted_funnel_ids": [FUNNEL_ID]},
                        "credentials": {"token_file_env": "SECRET"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    ai_path = root / "funnel_rule_registry.json"
    write_registry(ai_path)

    prompts_dir = root / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "business_v1.txt").write_text("Rule pack content must not appear in reports.", encoding="utf-8")

    config_dir = root / "config_funnels"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "business.yaml").write_text("funnel:\n  id: business\n", encoding="utf-8")

    return {
        "source": source_path,
        "video_dir": video_dir,
        "profiles": profiles_path,
        "channels": channels_path,
        "ai_registry": ai_path,
        "prompts": prompts_dir,
        "config_dir": config_dir,
    }


def _validator(paths: dict[str, Path]) -> FunnelValidator:
    return FunnelValidator(
        source_funnels_path=paths["source"],
        video_funnels_dir=paths["video_dir"],
        video_pipeline_profiles_path=paths["profiles"],
        output_channels_path=paths["channels"],
        ai_rule_registry_path=paths["ai_registry"],
        ai_prompts_dir=paths["prompts"],
        config_manager_funnels_dir=paths["config_dir"],
    )


def _canonical(**identity_overrides: object):
    return load_canonical_funnel(_valid_funnel_payload(**identity_overrides))


class TestBasicReport:
    def test_valid_funnel_with_dependencies_is_ready(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        report = _validator(paths).validate_funnel(_canonical())

        assert report.status == "ready"
        assert report.valid_config is True
        assert report.sync_ready is True
        assert report.processing_ready is True
        assert report.posting_ready is True
        assert report.runnable is True
        assert report.errors == ()
        assert report.checked_at.endswith("Z")

    def test_warnings_separated_from_errors(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        source = json.loads(paths["source"].read_text(encoding="utf-8"))
        source[0]["active"] = False
        paths["source"].write_text(json.dumps(source), encoding="utf-8")

        report = _validator(paths).validate_funnel(_canonical())
        assert report.errors == ()
        assert any(issue.code == "source_input_active_mismatch" for issue in report.warnings)
        assert all(issue.severity == FunnelValidationSeverity.WARNING for issue in report.warnings)


class TestPlaylistAcquisition:
    def test_playlist_funnel_with_active_source_is_processing_ready(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        payload = _valid_funnel_payload()
        payload["acquisition"]["source_type"] = "youtube_playlist"
        payload["acquisition"]["sources"] = [
            {
                "source_id": "fixture_playlist",
                "label": "Fixture Playlist",
                "url": "https://www.youtube.com/playlist?list=PLfixture",
                "source_type": "youtube_playlist",
                "active": True,
                "max_videos_per_source": 10,
                "hydrate_missing_duration": True,
                "title_allowlist": [],
                "title_blocklist": [],
            }
        ]
        source = json.loads(paths["source"].read_text(encoding="utf-8"))
        source[0]["source_type"] = "youtube_playlists"
        source[0]["sources"] = payload["acquisition"]["sources"]
        paths["source"].write_text(json.dumps(source), encoding="utf-8")
        report = _validator(paths).validate_funnel(load_canonical_funnel(payload))
        assert report.valid_config is True
        assert report.processing_ready is True
        assert not any(issue.code == "missing_active_source" for issue in report.errors)


class TestSchemaValidation:
    def test_invalid_raw_dict_returns_invalid_report(self) -> None:
        report = FunnelValidator().validate_funnel({"schema_version": 1, "identity": {"funnel_id": "bad-id"}})
        assert report.status == "invalid"
        assert report.valid_config is False
        assert report.runnable is False
        assert any(issue.code == "invalid_schema" for issue in report.errors)

    def test_no_active_sources_is_incomplete(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        payload = _valid_funnel_payload()
        payload["acquisition"]["sources"][0]["active"] = False
        report = _validator(paths).validate_funnel(payload)
        assert any(issue.code == "missing_active_source" for issue in report.errors)
        assert report.status == "invalid"
        assert report.runnable is False

    def test_disabled_funnel_not_runnable(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        report = _validator(paths).validate_funnel(_canonical(enabled=False))
        assert report.dependencies_ok is True
        assert report.runnable is False

    def test_archived_funnel_not_runnable(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        report = _validator(paths).validate_funnel(_canonical(status="archived"))
        assert report.runnable is False

    def test_testing_funnel_may_be_runnable(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        report = _validator(paths).validate_funnel(_canonical(status="testing"))
        assert report.runnable is True


class TestProcessingChecks:
    def test_missing_video_config_is_pending_sync(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        (paths["video_dir"] / f"{FUNNEL_ID}.json").unlink()
        report = _validator(paths).validate_funnel(_canonical())
        assert any(issue.code == "video_config_pending_sync" for issue in report.warnings)
        assert report.processing_ready is False

    def test_video_funnel_id_mismatch_is_error(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        video = json.loads((paths["video_dir"] / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        video["funnel_id"] = "other_funnel"
        (paths["video_dir"] / f"{FUNNEL_ID}.json").write_text(json.dumps(video), encoding="utf-8")
        report = _validator(paths).validate_funnel(_canonical())
        assert any(issue.code == "processing_funnel_id_mismatch" for issue in report.errors)

    def test_missing_pipeline_profile_when_different_from_funnel_id(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        payload = _valid_funnel_payload()
        payload["processing"]["pipeline_profile"] = "custom_profile"
        report = _validator(paths).validate_funnel(payload)
        assert any(issue.code == "missing_pipeline_profile" for issue in report.errors)

    def test_platform_mismatch_is_warning(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        payload = _valid_funnel_payload()
        payload["distribution"]["target_platforms"] = ["youtube_shorts", "tiktok"]
        report = _validator(paths).validate_funnel(payload)
        assert any(issue.code == "processing_platform_mismatch" for issue in report.warnings)


class TestAiChecks:
    def test_missing_ai_alias_is_pending_sync(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        doc = registry_document(include_funnel_alias=False)
        write_registry(paths["ai_registry"], doc)
        report = _validator(paths).validate_funnel(_canonical())
        assert any(issue.code == "ai_registry_pending_sync" for issue in report.warnings)
        assert report.sync_ready is True
        assert report.processing_ready is False
        assert report.processing_state == "pending_sync"

    def test_ai_profile_mismatch_is_error(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        payload = _valid_funnel_payload()
        payload["processing"]["ai_rules"]["ai_rule_profile"] = "finance"
        report = _validator(paths).validate_funnel(payload)
        assert any(issue.code == "ai_rule_profile_mismatch" for issue in report.errors)

    def test_missing_ai_prompt_file_is_error(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        (paths["prompts"] / "business_v1.txt").unlink()
        report = _validator(paths).validate_funnel(_canonical())
        assert any(issue.code == "missing_ai_prompt_file" for issue in report.errors)

    def test_valid_alias_and_prompt_passes(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        report = _validator(paths).validate_funnel(_canonical())
        assert not any(issue.code.startswith("missing_ai") for issue in report.errors)

    def test_custom_profile_pending_sync_is_warning(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        payload = _valid_funnel_payload()
        payload["processing"]["ai_rules"] = {
            "ai_rule_profile": "gaming",
            "prompt_managed": "custom",
            "prompt_text": "Select the best gaming clips.",
        }
        report = _validator(paths).validate_funnel(payload)
        assert not any(issue.code == "missing_ai_rule_alias" for issue in report.errors)
        assert any(issue.code == "ai_registry_pending_sync" for issue in report.warnings)


class TestDistributionChecks:
    def test_posting_enabled_without_accepting_route_is_error(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        channels = json.loads(paths["channels"].read_text(encoding="utf-8"))
        channels["channels"][0]["routing"]["accepted_funnel_ids"] = ["other_funnel"]
        paths["channels"].write_text(json.dumps(channels), encoding="utf-8")
        report = _validator(paths).validate_funnel(_canonical())
        assert any(issue.code == "channel_route_not_accepting_funnel" for issue in report.errors)
        assert any(issue.code == "missing_output_route" for issue in report.errors)

    def test_missing_channel_route_is_error(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        payload = _valid_funnel_payload()
        payload["distribution"]["channel_routes"] = [
            {"channel_id": "missing_channel", "platform": "youtube_shorts", "enabled": True}
        ]
        report = _validator(paths).validate_funnel(payload)
        assert any(issue.code == "channel_route_not_found" for issue in report.errors)

    def test_missing_channel_route_is_warning_when_posting_disabled(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        payload = _valid_funnel_payload()
        payload["distribution"]["posting_enabled"] = False
        payload["distribution"]["channel_routes"] = [
            {"channel_id": "missing_channel", "platform": "youtube_shorts", "enabled": True}
        ]
        report = _validator(paths).validate_funnel(payload)
        assert any(issue.code == "channel_route_not_found" for issue in report.warnings)
        assert not any(issue.code == "channel_route_not_found" for issue in report.errors)
        assert report.sync_ready is True

    def test_missing_output_channels_file_is_warning_when_posting_disabled(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        paths["channels"].unlink()
        payload = _valid_funnel_payload()
        payload["distribution"]["posting_enabled"] = False
        payload["distribution"]["channel_routes"] = []
        report = _validator(paths).validate_funnel(payload)
        assert any(
            issue.code == "missing_output_route" and issue.severity == "warning"
            for issue in report.warnings
        )
        assert report.sync_ready is True

    def test_route_not_accepting_funnel_is_warning_when_posting_disabled(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        channels = json.loads(paths["channels"].read_text(encoding="utf-8"))
        channels["channels"][0]["routing"]["accepted_funnel_ids"] = ["other_funnel"]
        paths["channels"].write_text(json.dumps(channels), encoding="utf-8")
        payload = _valid_funnel_payload()
        payload["distribution"]["posting_enabled"] = False
        report = _validator(paths).validate_funnel(payload)
        assert any(issue.code == "channel_route_not_accepting_funnel" for issue in report.warnings)
        assert report.processing_ready is True

    def test_target_platform_without_route_is_warning_when_posting_disabled(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        payload = _valid_funnel_payload()
        payload["distribution"]["posting_enabled"] = False
        payload["distribution"]["target_platforms"] = ["youtube_shorts", "tiktok"]
        report = _validator(paths).validate_funnel(payload)
        assert any(issue.code == "platform_without_route" for issue in report.warnings)

    def test_posting_disabled_without_routes_is_warning_not_error(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        payload = _valid_funnel_payload()
        payload["distribution"]["posting_enabled"] = False
        payload["distribution"]["channel_routes"] = []
        payload["distribution"]["target_platforms"] = []
        report = _validator(paths).validate_funnel(payload)
        assert not any(issue.code == "missing_output_route" and issue.severity == "error" for issue in report.errors)
        assert any(issue.code == "missing_output_route" for issue in report.warnings)


class TestConfigManagerChecks:
    def test_missing_config_manager_yaml_is_warning(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        (paths["config_dir"] / "business.yaml").unlink()
        report = _validator(paths).validate_funnel(_canonical())
        assert any(issue.code == "config_manager_yaml_pending_sync" for issue in report.warnings)
        assert report.sync_ready is True
        assert report.processing_ready is False
        assert report.processing_state == "pending_sync"

    def test_default_config_manager_mapping_uses_funnel_id(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        payload = _valid_funnel_payload()
        payload["mappings"]["config_manager_funnel_id"] = None
        report = _validator(paths).validate_funnel(payload)
        assert any(
            issue.code == "config_manager_yaml_pending_sync"
            and FUNNEL_ID in (issue.source or "")
            for issue in report.warnings
        )


class TestReadinessTiers:
    def _unsynced_custom_payload(self) -> dict:
        payload = _valid_funnel_payload()
        payload["processing"]["ai_rules"] = {
            "ai_rule_profile": "gaming",
            "prompt_managed": "custom",
            "prompt_text": "Select gaming highlights.",
        }
        payload["distribution"]["posting_enabled"] = False
        payload["distribution"]["channel_routes"] = []
        payload["mappings"] = {"config_manager_preset_id": "balanced", "config_manager_funnel_id": None}
        return payload

    def test_unsynced_custom_profile_is_sync_ready_not_processing_ready(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        (paths["video_dir"] / f"{FUNNEL_ID}.json").unlink(missing_ok=True)
        source = json.loads(paths["source"].read_text(encoding="utf-8"))
        paths["source"].write_text(
            json.dumps([item for item in source if item.get("funnel_id") != FUNNEL_ID]),
            encoding="utf-8",
        )
        (paths["config_dir"] / "business.yaml").unlink(missing_ok=True)
        report = _validator(paths).validate_funnel(self._unsynced_custom_payload())
        assert report.sync_ready is True
        assert report.processing_ready is False
        assert report.processing_state == "pending_sync"
        assert report.runnable is False

    def test_synced_custom_profile_becomes_processing_ready(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        payload = self._unsynced_custom_payload()
        payload["identity"]["funnel_id"] = "gaming_clips_001"
        payload["processing"]["pipeline_profile"] = "gaming_clips_001"
        funnel_id = "gaming_clips_001"
        registry_doc = registry_document(include_funnel_alias=False)
        registry_doc["profiles"]["gaming"] = {"rules_version": "gaming_v1", "managed": "ops_ui"}
        registry_doc["aliases"][funnel_id] = "gaming"
        write_registry(paths["ai_registry"], registry_doc)
        (paths["prompts"] / "gaming_v1.txt").write_text("rules", encoding="utf-8")
        (paths["video_dir"] / f"{funnel_id}.json").write_text(
            json.dumps({"funnel_id": funnel_id, "funnel_name": "Gaming", "platforms": {}}),
            encoding="utf-8",
        )
        source = json.loads(paths["source"].read_text(encoding="utf-8"))
        source.append({"funnel_id": funnel_id, "active": True, "sources": []})
        paths["source"].write_text(json.dumps(source), encoding="utf-8")
        (paths["config_dir"] / f"{funnel_id}.yaml").write_text("funnel:\n  id: gaming_clips_001\n", encoding="utf-8")
        report = _validator(paths).validate_funnel(payload)
        assert report.processing_ready is True

    def test_missing_prompt_blocks_processing_ready(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        (paths["prompts"] / "business_v1.txt").unlink()
        report = _validator(paths).validate_funnel(_canonical())
        assert report.processing_ready is False
        assert any(issue.code == "missing_ai_prompt_file" for issue in report.errors)

    def test_missing_prompt_blocks_sync(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        (paths["prompts"] / "business_v1.txt").unlink()
        report = _validator(paths).validate_funnel(_canonical())
        assert report.sync_ready is False
        assert any(issue.code == "missing_ai_prompt_file" for issue in report.errors)

    def test_missing_output_routes_do_not_block_processing_when_posting_disabled(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        payload = _valid_funnel_payload()
        payload["distribution"]["posting_enabled"] = False
        payload["distribution"]["channel_routes"] = []
        report = _validator(paths).validate_funnel(payload)
        assert report.processing_ready is True
        assert report.posting_state == "disabled"

    def test_missing_output_routes_block_posting_ready_when_posting_enabled(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        channels = json.loads(paths["channels"].read_text(encoding="utf-8"))
        channels["channels"][0]["routing"]["accepted_funnel_ids"] = []
        paths["channels"].write_text(json.dumps(channels), encoding="utf-8")
        report = _validator(paths).validate_funnel(_canonical())
        assert report.processing_ready is True
        assert report.posting_ready is False
        assert report.posting_state == "blocked"

    def test_enabled_testing_processing_ready_is_runnable(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        report = _validator(paths).validate_funnel(_canonical(status="testing"))
        assert report.processing_ready is True
        assert report.runnable is True

    def test_draft_is_not_runnable_even_when_processing_ready(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        report = _validator(paths).validate_funnel(_canonical(status="draft"))
        assert report.processing_ready is True
        assert report.runnable is False


class TestScopeProtection:
    def test_report_not_written_into_canonical_funnel(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        funnel = _canonical()
        report = _validator(paths).validate_funnel(funnel)
        dumped = dump_canonical_funnel(funnel)
        assert "readiness" not in dumped
        assert "operations" not in dumped
        assert report.status == "ready"

    def test_validator_does_not_modify_runtime_fixtures(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        before = {key: path.read_text(encoding="utf-8") for key, path in paths.items() if path.is_file()}
        _validator(paths).validate_funnel(_canonical())
        after = {key: path.read_text(encoding="utf-8") for key, path in paths.items() if path.is_file()}
        assert before == after

    def test_report_contains_no_prompt_text(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        report = _validator(paths).validate_funnel(_canonical())
        payload = json.dumps(
            {
                "errors": [issue.__dict__ for issue in report.errors],
                "warnings": [issue.__dict__ for issue in report.warnings],
                "info": [issue.__dict__ for issue in report.info],
            }
        )
        assert "Rule pack content" not in payload
        assert "prompt_text" not in payload

    def test_report_contains_no_oauth_credentials(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        report = _validator(paths).validate_funnel(_canonical())
        messages = [issue.message for issue in (*report.errors, *report.warnings, *report.info)]
        assert "SECRET" not in messages
        assert not any("oauth" in message.lower() for message in messages)
        assert not any("credentials" in message.lower() for message in messages)

    def test_report_has_no_operations_fields(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        report = _validator(paths).validate_funnel(_canonical())
        codes = [issue.code for issue in (*report.errors, *report.warnings, *report.info)]
        assert not any(code.startswith("can_") for code in codes)
        assert "pause_state" not in codes
        assert "operations" not in codes

    def test_missing_output_path_warns_not_crashes(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        validator = FunnelValidator(
            source_funnels_path=paths["source"],
            video_funnels_dir=paths["video_dir"],
            video_pipeline_profiles_path=paths["profiles"],
            ai_rule_registry_path=paths["ai_registry"],
            ai_prompts_dir=paths["prompts"],
        )
        report = validator.validate_funnel(_canonical())
        assert any(issue.code == "output_channels_not_checked" for issue in report.warnings)


class TestAiRegistryPermissionErrors:
    def test_permission_denied_becomes_validation_error(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        os.chmod(paths["ai_registry"], 0o000)
        try:
            report = _validator(paths).validate_funnel(_canonical())
        finally:
            os.chmod(paths["ai_registry"], 0o644)
        assert any(issue.code == "ai_registry_unreadable" for issue in report.errors)
        assert report.sync_ready is False
        assert report.processing_ready is False

    def test_readable_registry_still_validates(self, tmp_path: Path) -> None:
        paths = _write_dependency_fixtures(tmp_path)
        report = _validator(paths).validate_funnel(_canonical())
        assert not any(issue.code == "ai_registry_unreadable" for issue in report.errors)
