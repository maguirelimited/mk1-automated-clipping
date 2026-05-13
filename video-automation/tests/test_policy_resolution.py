import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from pipeline_utils import resolve_pipeline_run_policy  # noqa: E402


def test_http_selection_overrides_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("VIDEO_PIPELINE_RUNTIME_JSON", raising=False)
    monkeypatch.delenv("MK04_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("MK04_SELECTION_MODEL", raising=False)
    cfg_path = tmp_path / "pipeline_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "selection": {
                    "max_clips": 3,
                    "min_clip_duration_sec": 10,
                    "max_clip_duration_sec": 20,
                },
                "chunking": {},
                "models": {},
            }
        ),
        encoding="utf-8",
    )
    bundle = resolve_pipeline_run_policy(
        pipeline_config_abs=str(cfg_path.resolve()),
        pipeline_config=json.loads(cfg_path.read_text(encoding="utf-8")),
        pipeline_profile=None,
        request_pipeline_blob={},
        request_selection_blob={
            "min_duration_sec": 30,
            "max_duration_sec": 55,
            "max_clips": 5,
        },
    )
    assert bundle["selection"]["min_duration_sec"] == 30.0
    assert bundle["selection"]["max_duration_sec"] == 55.0
    assert bundle["selection"]["max_clips"] == 5
    audit = bundle["policy_audit"]
    assert audit["selection_key_sources"]["min_duration_sec"] == "http_selection"
    assert audit["selection_key_sources"]["max_duration_sec"] == "http_selection"
    assert audit["selection_key_sources"]["max_clips"] == "http_selection"
    assert audit["http_execution_overrides"]["had_any_http_execution_overrides"] is True
    assert audit["deterministic_without_http_or_infra_env"] is False


def test_named_profile_merges_before_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("VIDEO_PIPELINE_RUNTIME_JSON", raising=False)
    monkeypatch.delenv("MK04_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("MK04_SELECTION_MODEL", raising=False)
    cfg_path = tmp_path / "pipeline_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "selection": {
                    "max_clips": 2,
                    "min_clip_duration_sec": 15,
                    "max_clip_duration_sec": 35,
                    "max_overlap_sec": 1,
                },
                "chunking": {"enabled": False},
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
                    "funnel_alpha": {"selection": {"max_clips": 7, "min_duration_sec": 18}},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIDEO_PIPELINE_PROFILES_PATH", str(prof_path.resolve()))
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    bundle = resolve_pipeline_run_policy(
        pipeline_config_abs=str(cfg_path.resolve()),
        pipeline_config=cfg,
        pipeline_profile="funnel_alpha",
        request_pipeline_blob={},
        request_selection_blob={"max_clips": 9},
    )

    assert bundle["selection"]["max_clips"] == 9
    assert bundle["selection"]["min_duration_sec"] == 18.0
    assert bundle["policy_audit"]["selection_key_sources"]["min_duration_sec"] == "legacy_profile"
    assert bundle["policy_audit"]["selection_key_sources"]["max_clips"] == "http_selection"
    chunk = bundle["chunking_effective"]
    assert "enabled" in chunk


def test_repo_default_pipeline_profile_without_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("VIDEO_PIPELINE_RUNTIME_JSON", raising=False)
    monkeypatch.delenv("MK04_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("MK04_SELECTION_MODEL", raising=False)

    cfg_path = tmp_path / "pipeline_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "defaults": {"pipeline_profile": "solo_funnel"},
                "selection": {
                    "max_clips": 1,
                    "min_clip_duration_sec": 5,
                    "max_clip_duration_sec": 9,
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
        json.dumps({"profiles": {"solo_funnel": {"selection": {"max_clips": 42}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIDEO_PIPELINE_PROFILES_PATH", str(prof_path.resolve()))
    bundle = resolve_pipeline_run_policy(
        pipeline_config_abs=str(cfg_path.resolve()),
        pipeline_config=json.loads(cfg_path.read_text(encoding="utf-8")),
        pipeline_profile=None,
        request_pipeline_blob={},
        request_selection_blob={},
    )
    assert bundle["selection"]["max_clips"] == 42
    pa = bundle["policy_audit"]
    assert pa["pipeline_profile_resolve_source"] == "config_default"
    assert pa["pipeline_profile_apply_config_default_used"] is True
    assert pa["pipeline_profile_resolved"] == "solo_funnel"
    assert pa["http_execution_overrides"]["had_any_http_execution_overrides"] is False
    assert pa["deterministic_without_http_or_infra_env"] is True


@pytest.mark.parametrize(
    ("min_v", "max_v"),
    [(40.0, 20.0), (5.0, 3.0)],
)
def test_invalid_duration_raises(tmp_path: Path, min_v: float, max_v: float):
    cfg_path = tmp_path / "pipeline.json"
    cfg_path.write_text(
        json.dumps(
            {
                "selection": {
                    "min_clip_duration_sec": 5,
                    "max_clip_duration_sec": 30,
                    "max_clips": 2,
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="min_duration_sec"):
        resolve_pipeline_run_policy(
            pipeline_config_abs=str(cfg_path),
            pipeline_config=cfg,
            pipeline_profile=None,
            request_pipeline_blob={},
            request_selection_blob={
                "min_duration_sec": min_v,
                "max_duration_sec": max_v,
            },
        )


def test_unknown_profile_via_http_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("VIDEO_PIPELINE_RUNTIME_JSON", raising=False)
    monkeypatch.delenv("MK04_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("MK04_SELECTION_MODEL", raising=False)

    prof_path = tmp_path / "video_pipeline_profiles.json"
    prof_path.write_text(json.dumps({"profiles": {}}), encoding="utf-8")
    monkeypatch.setenv("VIDEO_PIPELINE_PROFILES_PATH", str(prof_path.resolve()))

    cfg_path = tmp_path / "pipeline.json"
    cfg_path.write_text(
        json.dumps(
            {
                "selection": {
                    "max_clips": 3,
                    "min_clip_duration_sec": 6,
                    "max_clip_duration_sec": 20,
                }
            }
        ),
        encoding="utf-8",
    )
    bundle = resolve_pipeline_run_policy(
        pipeline_config_abs=str(cfg_path.resolve()),
        pipeline_config=json.loads(cfg_path.read_text(encoding="utf-8")),
        pipeline_profile="ghost_profile",
        request_pipeline_blob={},
        request_selection_blob={},
    )
    warns = bundle["policy_audit"]["warnings"]
    msg = "".join(str(w) for w in warns)
    assert "ghost_profile" in msg or "ghost_profile".lower() in msg.lower()
    assert "profiles" in msg.lower()
    assert bundle["policy_audit"]["pipeline_profile_resolved"] is None
    assert bundle["policy_audit"]["pipeline_profile_resolve_source"] == "http"
