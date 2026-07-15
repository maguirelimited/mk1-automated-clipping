"""Tests for canonical funnel dependency path resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops_ui.funnel_management.dependency_paths import resolve_funnel_dependency_paths
from ops_ui.funnel_management.readiness_summary import build_simple_funnel_status
from ops_ui.funnel_management.schema import load_canonical_funnel
from ops_ui.funnel_management.sync import FunnelSyncTargetPaths, FunnelSynchronizer
from ops_ui.funnels import build_funnel_validator
from tests.funnel_registry_fixtures import write_registry


FUNNEL_ID = "gaming_clips_dev_001"


def _posting_disabled_payload() -> dict:
    return {
        "schema_version": 1,
        "identity": {
            "funnel_id": FUNNEL_ID,
            "display_name": "Gaming Clips Dev",
            "description": "Processing-only acceptance funnel",
            "category": "gaming",
            "enabled": True,
            "environment": "dev",
            "status": "testing",
            "template_source": "baseline_stream_clips",
            "created_at": "2026-07-08T00:00:00Z",
            "updated_at": "2026-07-08T00:00:00Z",
            "operator_note": None,
        },
        "acquisition": {
            "source_type": "youtube_playlist",
            "sources": [
                {
                    "source_id": f"{FUNNEL_ID}_source",
                    "label": "Playlist",
                    "url": "https://www.youtube.com/watch?v=abc&list=PLtest",
                    "source_type": "youtube_playlist",
                    "active": True,
                    "max_videos_per_source": 25,
                    "hydrate_missing_duration": True,
                    "title_allowlist": [],
                    "title_blocklist": [],
                }
            ],
            "min_duration_minutes": 5,
            "max_duration_minutes": 180,
            "max_downloads_per_run": 1,
        },
        "processing": {
            "pipeline_profile": FUNNEL_ID,
            "ai_rules": {"ai_rule_profile": "business", "prompt_managed": "builtin"},
            "selection": {
                "max_clips": 2,
                "min_clip_duration_sec": 10,
                "max_clip_duration_sec": 90,
                "max_overlap_sec": 2,
            },
            "output": {
                "filename_prefix": "gaming-clips-dev",
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
            "posting_enabled": False,
            "posting_mode": "disabled",
            "target_platforms": ["youtube_shorts"],
            "channel_routes": [],
        },
        "mappings": {
            "config_manager_funnel_id": FUNNEL_ID,
            "config_manager_preset_id": "balanced",
        },
    }


@pytest.fixture
def runtime_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    root = tmp_path / "etc" / "mk04" / "dev"
    source_dir = root / "source-input"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "funnels.json"
    source_path.write_text("[]", encoding="utf-8")
    video_dir = root / "video-automation" / "funnels"
    video_dir.mkdir(parents=True)
    profiles_path = root / "video-automation" / "video_pipeline_profiles.json"
    profiles_path.write_text(json.dumps({"profiles": {}}), encoding="utf-8")
    channels_path = root / "output-funnel" / "channels.json"
    channels_path.parent.mkdir(parents=True)
    channels_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    ai_registry = tmp_path / "funnel_rule_registry.json"
    write_registry(ai_registry)
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "business_v1.txt").write_text("rules", encoding="utf-8")
    config_dir = tmp_path / "config_funnels"
    config_dir.mkdir()

    monkeypatch.setenv("MK04_ENV", "dev")
    monkeypatch.setenv("MK04_CONFIG_ROOT", str(root))
    monkeypatch.setenv("INPUT_SERVICE_CONFIG_DIR", str(source_dir))
    monkeypatch.setenv("FUNNEL_CONFIG_DIR", str(video_dir))
    monkeypatch.setenv("VIDEO_PIPELINE_PROFILES_PATH", str(profiles_path))
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


def _sync_paths(deps) -> FunnelSyncTargetPaths:
    return FunnelSyncTargetPaths(
        source_funnels_path=deps.source_funnels_path,
        video_funnels_dir=deps.video_funnels_dir,
        output_channels_path=deps.output_channels_path,
        ai_rule_registry_path=deps.ai_rule_registry_path,
        ai_prompts_dir=deps.ai_prompts_dir,
        config_manager_funnels_dir=deps.config_manager_funnels_dir,
    )


def test_validator_and_sync_share_source_input_path(runtime_env: dict[str, Path]) -> None:
    deps = resolve_funnel_dependency_paths()
    assert deps.source_funnels_path == runtime_env["source"]
    assert build_funnel_validator().source_funnels_path == runtime_env["source"]


def test_posting_disabled_funnel_is_processing_ready_after_sync(runtime_env: dict[str, Path]) -> None:
    funnel = load_canonical_funnel(_posting_disabled_payload())
    deps = resolve_funnel_dependency_paths()
    before = build_funnel_validator().validate_funnel(funnel)
    assert before.processing_ready is False
    assert any(issue.code == "source_input_pending_sync" for issue in before.warnings)

    sync_report = FunnelSynchronizer(_sync_paths(deps)).apply(funnel)
    assert sync_report.ok is True

    after = build_funnel_validator().validate_funnel(funnel)
    assert after.processing_ready is True
    assert after.runnable is True

    status = build_simple_funnel_status(
        posting_enabled=False,
        identity_status="testing",
        identity_enabled=True,
        report=after,
        ops={"can_run": True, "paused": False},
    )
    assert status["processing_label"] == "Ready"
    assert status["test_run_available"] is True
    assert status["blockers"] == []
