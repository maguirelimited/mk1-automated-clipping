"""Content funnel config merge (video-automation ``config/funnels``)."""

import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from funnel_config import parse_content_funnel_dict, sanitize_funnel_config_basename  # noqa: E402
from pipeline_utils import resolve_pipeline_run_policy  # noqa: E402


def test_funnel_config_selection_overlays_after_legacy_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("VIDEO_PIPELINE_RUNTIME_JSON", raising=False)
    monkeypatch.delenv("MK04_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("MK04_SELECTION_MODEL", raising=False)

    cfg_path = tmp_path / "pipeline_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "defaults": {"pipeline_profile": "pf1"},
                "selection": {
                    "max_clips": 1,
                    "min_clip_duration_sec": 10,
                    "max_clip_duration_sec": 90,
                    "max_overlap_sec": 0,
                },
                "chunking": {},
                "models": {},
            }
        ),
        encoding="utf-8",
    )
    prof_path = tmp_path / "video_pipeline_profiles.json"
    prof_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "pf1": {
                        "selection": {
                            "max_clips": 3,
                            "min_duration_sec": 12,
                            "max_duration_sec": 80,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIDEO_PIPELINE_PROFILES_PATH", str(prof_path.resolve()))

    funnels_dir = tmp_path / "funnels"
    funnels_dir.mkdir()
    funnel_path = funnels_dir / "overlay_funnel.json"
    funnel_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "funnel_id": "overlay_funnel",
                "funnel_name": "Overlay",
                "platforms": {"tiktok": True, "instagram_reels": False, "youtube_shorts": False, "x": False},
                "selection": {"max_clips": 7, "min_duration_sec": 25, "max_duration_sec": 40},
                "output": {"filename_prefix": "ov", "delivery_mode": "pull_from_output_endpoint"},
            }
        ),
        encoding="utf-8",
    )

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    bundle = resolve_pipeline_run_policy(
        pipeline_config_abs=str(cfg_path.resolve()),
        pipeline_config=cfg,
        pipeline_profile=None,
        request_pipeline_blob={},
        request_selection_blob={},
        http_funnel_id="overlay_funnel",
    )

    assert bundle["selection"]["max_clips"] == 7
    assert bundle["selection"]["min_duration_sec"] == 25.0
    assert bundle["selection"]["max_duration_sec"] == 40.0
    src = bundle["policy_audit"]["selection_key_sources"]
    assert src["max_clips"] == "funnel_config"
    assert src["min_duration_sec"] == "funnel_config"
    assert src["max_duration_sec"] == "funnel_config"
    assert bundle["funnel_ops"]["funnel_id"] == "overlay_funnel"
    assert bundle["funnel_ops"]["output"]["filename_prefix"] == "ov"
    assert bundle["funnel_ops"]["platforms"]["tiktok"] is True
    assert bundle["funnel_ops"]["platforms"]["x"] is False


def test_default_funnel_id_loads_without_http_funnel_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("VIDEO_PIPELINE_RUNTIME_JSON", raising=False)
    monkeypatch.delenv("MK04_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("MK04_SELECTION_MODEL", raising=False)

    cfg_path = tmp_path / "pipeline_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "defaults": {"default_funnel_id": "quiet_funnel"},
                "selection": {
                    "max_clips": 9,
                    "min_clip_duration_sec": 5,
                    "max_clip_duration_sec": 60,
                },
                "chunking": {},
                "models": {},
            }
        ),
        encoding="utf-8",
    )
    prof_path = tmp_path / "video_pipeline_profiles.json"
    prof_path.write_text(json.dumps({"profiles": {}}), encoding="utf-8")
    monkeypatch.setenv("VIDEO_PIPELINE_PROFILES_PATH", str(prof_path.resolve()))

    funnels_dir = tmp_path / "funnels"
    funnels_dir.mkdir()
    (funnels_dir / "quiet_funnel.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "funnel_id": "quiet_funnel",
                "funnel_name": "Quiet",
                "platforms": {"tiktok": False, "instagram_reels": False, "youtube_shorts": False, "x": True},
                "selection": {"max_clips": 2},
                "output": {},
            }
        ),
        encoding="utf-8",
    )

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    bundle = resolve_pipeline_run_policy(
        pipeline_config_abs=str(cfg_path.resolve()),
        pipeline_config=cfg,
        pipeline_profile=None,
        request_pipeline_blob={},
        request_selection_blob={},
        http_funnel_id=None,
    )
    assert bundle["selection"]["max_clips"] == 2
    assert bundle["policy_audit"]["selection_key_sources"]["max_clips"] == "funnel_config"
    fr = bundle["policy_audit"]["funnel_resolution"]
    assert fr["funnel_resolve_source"] == "defaults.default_funnel_id"
    assert fr["funnel_config_applied"] is True


def test_explicit_funnel_id_missing_file_warns_no_funnel_layer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("VIDEO_PIPELINE_RUNTIME_JSON", raising=False)
    monkeypatch.delenv("MK04_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("MK04_SELECTION_MODEL", raising=False)

    cfg_path = tmp_path / "pipeline_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "selection": {
                    "max_clips": 4,
                    "min_clip_duration_sec": 6,
                    "max_clip_duration_sec": 20,
                },
                "chunking": {},
                "models": {},
            }
        ),
        encoding="utf-8",
    )
    prof_path = tmp_path / "video_pipeline_profiles.json"
    prof_path.write_text(json.dumps({"profiles": {}}), encoding="utf-8")
    monkeypatch.setenv("VIDEO_PIPELINE_PROFILES_PATH", str(prof_path.resolve()))

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    bundle = resolve_pipeline_run_policy(
        pipeline_config_abs=str(cfg_path.resolve()),
        pipeline_config=cfg,
        pipeline_profile="ghost_only",
        request_pipeline_blob={},
        request_selection_blob={},
        http_funnel_id="no_such_file_funnel",
    )
    assert bundle["funnel_ops"] is None
    assert bundle["selection"]["max_clips"] == 4
    warns = bundle["policy_audit"]["warnings"]
    assert any("No content funnel file" in str(w) for w in warns)


def test_parse_content_funnel_rejects_bad_prefix():
    with pytest.raises(ValueError, match="filename_prefix"):
        parse_content_funnel_dict(
            {
                "schema_version": 1,
                "funnel_id": "x",
                "funnel_name": "X",
                "platforms": {"enabled": ["tiktok"]},
                "output": {"filename_prefix": "bad prefix"},
            },
            source_path="/tmp/x.json",
            expected_funnel_id="x",
        )


def test_parse_content_funnel_rejects_unknown_platform():
    with pytest.raises(ValueError, match="unknown platform"):
        parse_content_funnel_dict(
            {
                "schema_version": 1,
                "funnel_id": "x",
                "funnel_name": "X",
                "platforms": {"enabled": ["facebook"]},
            },
            source_path="/tmp/x.json",
            expected_funnel_id="x",
        )


def test_parse_content_funnel_boolean_platforms_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown platforms key"):
        parse_content_funnel_dict(
            {
                "funnel_id": "x",
                "funnel_name": "X",
                "platforms": {"tiktok": True, "facebook": True},
            },
            source_path="/tmp/x.json",
            expected_funnel_id="x",
        )


def test_parse_content_funnel_schema_version_optional():
    cfg = parse_content_funnel_dict(
        {
            "funnel_id": "z",
            "funnel_name": "Z",
            "platforms": {"tiktok": True, "instagram_reels": False, "youtube_shorts": False, "x": False},
        },
        source_path="/tmp/z.json",
        expected_funnel_id="z",
    )
    assert cfg.funnel_id == "z"


def test_http_funnel_config_stem_overrides_http_funnel_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("VIDEO_PIPELINE_RUNTIME_JSON", raising=False)
    monkeypatch.delenv("MK04_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("MK04_SELECTION_MODEL", raising=False)

    cfg_path = tmp_path / "pipeline_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "selection": {
                    "max_clips": 1,
                    "min_clip_duration_sec": 10,
                    "max_clip_duration_sec": 90,
                },
                "chunking": {},
                "models": {},
            }
        ),
        encoding="utf-8",
    )
    prof_path = tmp_path / "video_pipeline_profiles.json"
    prof_path.write_text(json.dumps({"profiles": {}}), encoding="utf-8")
    monkeypatch.setenv("VIDEO_PIPELINE_PROFILES_PATH", str(prof_path.resolve()))

    funnels_dir = tmp_path / "funnels"
    funnels_dir.mkdir()
    (funnels_dir / "alpha.json").write_text(
        json.dumps(
            {
                "funnel_id": "alpha",
                "funnel_name": "A",
                "platforms": {"tiktok": True, "instagram_reels": False, "youtube_shorts": False, "x": False},
                "selection": {"max_clips": 3},
                "output": {},
            }
        ),
        encoding="utf-8",
    )
    (funnels_dir / "beta.json").write_text(
        json.dumps(
            {
                "funnel_id": "beta",
                "funnel_name": "B",
                "platforms": {"tiktok": False, "instagram_reels": False, "youtube_shorts": False, "x": False},
                "selection": {"max_clips": 9},
                "output": {},
            }
        ),
        encoding="utf-8",
    )

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    bundle = resolve_pipeline_run_policy(
        pipeline_config_abs=str(cfg_path.resolve()),
        pipeline_config=cfg,
        pipeline_profile=None,
        request_pipeline_blob={},
        request_selection_blob={},
        http_funnel_id="beta",
        http_funnel_config="alpha.json",
    )
    assert bundle["selection"]["max_clips"] == 3
    assert bundle["funnel_ops"]["funnel_id"] == "alpha"
    assert bundle["policy_audit"]["funnel_resolution"]["funnel_resolve_source"] == "http_funnel_config"


def test_sanitize_funnel_config_basename_accepts_json_suffix():
    assert sanitize_funnel_config_basename("foo.json") == "foo"
    assert sanitize_funnel_config_basename(None) is None
