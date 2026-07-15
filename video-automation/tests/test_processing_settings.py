from __future__ import annotations

import importlib
import json
import os
import sys

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import processing_settings  # noqa: E402


def _reload():
    return importlib.reload(processing_settings)


def _write_controls(tmp_path, processing=None, post=None):
    path = tmp_path / "controls.json"
    path.write_text(
        json.dumps(
            {
                "ingestion_paused": False,
                "processing_config": processing or {},
                "post_processing_config": post or {},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_pipeline_mode_default_is_legacy(monkeypatch, tmp_path):
    monkeypatch.delenv("PROCESSING_PIPELINE_MODE", raising=False)
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(tmp_path / "missing.json"))
    mod = _reload()
    assert mod.resolve_pipeline_mode() == "legacy"


def test_pipeline_mode_from_ui(monkeypatch, tmp_path):
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(_write_controls(tmp_path, processing={"processing_pipeline_mode": "mk1"})))
    mod = _reload()
    assert mod.resolve_pipeline_mode() == "mk1"


def test_pipeline_mode_per_run_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(_write_controls(tmp_path, processing={"processing_pipeline_mode": "legacy"})))
    mod = _reload()
    assert mod.resolve_pipeline_mode(per_run="mk1") == "mk1"


def test_mk1_defaults_when_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.delenv("PROCESSING_SECTION_OVERLAP_SEC", raising=False)
    monkeypatch.delenv("PROCESSING_MAX_CANDIDATES_PER_SECTION", raising=False)
    mod = _reload()
    assert mod.resolve_sectioning_config()["overlap_sec"] == 60.0
    assert mod.resolve_discovery_config()["max_candidates_per_section"] == 5


def test_explicit_saved_values_override_mk1_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MK04_CONTROLS_FILE",
        str(
            _write_controls(
                tmp_path,
                processing={
                    "section_overlap_sec": "30",
                    "max_candidates_per_section": "3",
                },
            )
        ),
    )
    mod = _reload()
    assert mod.resolve_sectioning_config()["overlap_sec"] == 30.0
    assert mod.resolve_discovery_config()["max_candidates_per_section"] == 3


def test_sectioning_and_discovery_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MK04_CONTROLS_FILE",
        str(
            _write_controls(
                tmp_path,
                processing={
                    "section_target_duration_sec": "240",
                    "max_candidates_per_section": "2",
                    "discovery_fail_fast": "true",
                },
            )
        ),
    )
    mod = _reload()
    sectioning = mod.resolve_sectioning_config()
    discovery = mod.resolve_discovery_config()
    assert sectioning["target_section_duration_sec"] == 240.0
    assert sectioning["max_section_duration_sec"] == 420.0  # default preserved
    assert discovery["max_candidates_per_section"] == 2
    assert discovery["fail_fast"] is True


def test_selection_and_conveyor_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("POST_PROCESSING_MAX_CLIPS", raising=False)
    monkeypatch.setenv(
        "MK04_CONTROLS_FILE",
        str(
            _write_controls(
                tmp_path,
                post={
                    "selection_mode": "growth",
                    "max_clips": "8",
                    "post_processing_enabled": "false",
                    "format_target_width": "720",
                    "captions_font_size": "72",
                    "captions_enable_keyword_highlighting": "true",
                },
            )
        ),
    )
    mod = _reload()
    assert mod.resolve_post_processing_enabled() is False
    selection = mod.resolve_selection_config()
    assert selection["selection_mode"] == "growth"
    assert selection["max_clips"] == 8
    assert selection["min_confidence"] == 0.6  # default preserved
    conveyor = mod.resolve_conveyor_config()
    assert conveyor["target_width"] == 720
    assert conveyor["font_size"] == 72
    assert conveyor["enable_keyword_highlighting"] is True
    assert conveyor["video_codec"] == "libx264"  # default preserved
    assert conveyor["reframe_mode"] == "blur_background"
    assert conveyor["face_track_test_enabled"] is False


def test_conveyor_face_track_test_enabled_from_ui(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MK04_CONTROLS_FILE",
        str(
            _write_controls(
                tmp_path,
                post={"format_face_track_test_enabled": "true"},
            )
        ),
    )
    mod = _reload()
    assert mod.resolve_conveyor_config()["face_track_test_enabled"] is True


def test_env_fallback_when_no_ui(monkeypatch, tmp_path):
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("POST_PROCESSING_MAX_CLIPS", "4")
    mod = _reload()
    assert mod.resolve_selection_config()["max_clips"] == 4


def test_conveyor_reframe_mode_from_ui(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MK04_CONTROLS_FILE",
        str(
            _write_controls(
                tmp_path,
                post={"format_reframe_mode": "auto"},
            )
        ),
    )
    mod = _reload()
    assert mod.resolve_conveyor_config()["reframe_mode"] == "auto"


def test_conveyor_reframe_mode_invalid_ui_value_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MK04_CONTROLS_FILE",
        str(
            _write_controls(
                tmp_path,
                post={"format_reframe_mode": "smart_ai_magic"},
            )
        ),
    )
    mod = _reload()
    assert mod.resolve_conveyor_config()["reframe_mode"] == "blur_background"
