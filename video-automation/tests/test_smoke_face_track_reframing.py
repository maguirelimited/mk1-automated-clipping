"""Tests for smoke_face_track_reframing harness."""

from __future__ import annotations

import json
import os
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SMOKE_DIR = SCRIPTS_DIR / "smoke"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(SMOKE_DIR) not in sys.path:
    sys.path.insert(0, str(SMOKE_DIR))

import smoke_face_track_reframing as smoke  # noqa: E402
from post_processing_modules import MODULE_STATUS_FAIL, MODULE_STATUS_PASS  # noqa: E402
from reframing.config import DEFAULT_REFRAME_MODE, FORMAT_STRATEGY_BLURRED_BACKGROUND  # noqa: E402


def test_parse_args_defaults():
    args = smoke.parse_args(["--input", "/in.mp4", "--output-dir", "/out"])
    assert args.input == "/in.mp4"
    assert args.output_dir == "/out"
    assert args.modes is None


def test_parse_args_multiple_modes():
    args = smoke.parse_args(
        [
            "--input",
            "/in.mp4",
            "--output-dir",
            "/out",
            "--mode",
            "blur_background",
            "--mode",
            "auto",
        ]
    )
    assert args.modes == ["blur_background", "auto"]


def test_run_smoke_missing_input_raises(tmp_path):
    with pytest.raises(smoke.SmokeHarnessError):
        smoke.run_smoke(
            Namespace(
                input=str(tmp_path / "missing.mp4"),
                output_dir=str(tmp_path / "out"),
                modes=["blur_background"],
                job_id="job",
                candidate_id="cand",
                duration_tolerance_sec=0.25,
                skip_pipeline_sidecars=True,
            )
        )


def test_output_directory_and_report_written(tmp_path):
    input_path = tmp_path / "in.mp4"
    input_path.write_bytes(b"\x00" * 128)
    output_dir = tmp_path / "out"

    pass_metadata = {
        "format_strategy": "blurred_background_fit_foreground",
        "reframe_mode": "blur_background",
        "reframe_attempted": False,
        "target_width": 1080,
        "target_height": 1920,
    }
    module_output = output_dir / "tmp" / "blur_background" / "job_cand_platform_safe_format_v1.mp4"
    module_output.parent.mkdir(parents=True, exist_ok=True)
    module_output.write_bytes(b"\x00" * 256)

    with patch.object(smoke, "probe_input_info", return_value={
        "input_path": str(input_path),
        "width": 640,
        "height": 360,
        "duration_sec": 2.0,
        "has_audio": True,
    }), patch.object(smoke, "collect_pipeline_artifacts", return_value=None), patch(
        "smoke_face_track_reframing.PlatformSafeFormatV1Module"
    ) as mock_module_cls, patch(
        "smoke_face_track_reframing._probe_video_info",
        return_value={"width": 1080, "height": 1920, "duration_sec": 2.0, "has_audio": True},
    ), patch.object(smoke, "mediapipe_import_status", return_value=(False, "missing")), patch.object(
        smoke, "detector_backend_available", return_value=(False, "missing")
    ):
        mock_module_cls.return_value.run.return_value = {
            "status": MODULE_STATUS_PASS,
            "output_path": str(module_output),
            "warnings": [],
            "metadata": pass_metadata,
        }
        report = smoke.run_smoke(
            Namespace(
                input=str(input_path),
                output_dir=str(output_dir),
                modes=["blur_background"],
                job_id="job",
                candidate_id="cand",
                duration_tolerance_sec=0.25,
                skip_pipeline_sidecars=True,
            )
        )

    assert (output_dir / "smoke_report.json").is_file()
    assert (output_dir / "input_info.json").is_file()
    assert report["production_default_reframe_mode"] == DEFAULT_REFRAME_MODE
    assert report["summary"]["blur_background"]["checks_passed"] is True
    assert (output_dir / "blur_background.mp4").is_file()


def test_face_track_strict_failure_recorded(tmp_path):
    checks = smoke.validate_mode_output(
        mode="face_track",
        reframe_mode="face_track",
        face_track_test_enabled=False,
        result={
            "status": MODULE_STATUS_FAIL,
            "output_path": None,
            "warnings": [],
            "metadata": {
                "failure_code": "face_track_pipeline_failed",
                "format_strategy": "face_track_crop",
            },
        },
        input_info={"duration_sec": 2.0, "has_audio": True},
        duration_tolerance_sec=0.25,
        target_width=1080,
        target_height=1920,
    )
    assert checks["expected_strict_failure"] is True
    assert checks["failure_code"] == "face_track_pipeline_failed"


def test_auto_blur_fallback_recorded(tmp_path):
    (tmp_path / "out.mp4").write_bytes(b"1")
    with patch("smoke_face_track_reframing._probe_video_info", return_value={
        "width": 1080,
        "height": 1920,
        "duration_sec": 2.0,
        "has_audio": False,
    }):
        checks = smoke.validate_mode_output(
            mode="auto",
            reframe_mode="auto",
            face_track_test_enabled=True,
            result={
                "status": MODULE_STATUS_PASS,
                "output_path": str(tmp_path / "out.mp4"),
                "warnings": ["face_detection_failed_using_blur_fallback"],
                "metadata": {
                    "format_strategy": FORMAT_STRATEGY_BLURRED_BACKGROUND,
                    "reframe_attempted": True,
                    "face_track_attempted": True,
                },
            },
            input_info={"duration_sec": 2.0, "has_audio": False},
            duration_tolerance_sec=0.25,
            target_width=1080,
            target_height=1920,
        )
    assert checks["format_strategy_ok"] is True


def test_auto_test_disabled_validation():
    checks = smoke.validate_mode_output(
        mode="auto_test_disabled",
        reframe_mode="auto",
        face_track_test_enabled=False,
        result={
            "status": MODULE_STATUS_PASS,
            "output_path": "/tmp/out.mp4",
            "warnings": ["face_track_test_disabled_using_blur_fallback"],
            "metadata": {
                "format_strategy": FORMAT_STRATEGY_BLURRED_BACKGROUND,
                "face_track_attempted": False,
                "face_track_skip_reason": "face_track_test_disabled",
            },
        },
        input_info={"duration_sec": 2.0, "has_audio": False},
        duration_tolerance_sec=0.25,
        target_width=1080,
        target_height=1920,
    )
    assert checks["format_strategy_ok"] is True
    assert checks["face_track_attempted"] is False


def test_resolve_smoke_mode_config_matrix():
    args = smoke.parse_args(["--input", "/in.mp4", "--output-dir", "/out"])
    assert smoke.resolve_smoke_mode_config("auto_test_disabled", args) == ("auto", False)
    assert smoke.resolve_smoke_mode_config("auto_test_enabled", args) == ("auto", True)
    assert smoke.resolve_smoke_mode_config("auto", args) == ("auto", False)
    args_enabled = smoke.parse_args(
        ["--input", "/in.mp4", "--output-dir", "/out", "--face-track-test-enabled"]
    )
    assert smoke.resolve_smoke_mode_config("auto", args_enabled) == ("auto", True)


def test_collect_pipeline_artifacts_handles_mediapipe_unavailable(tmp_path):
    input_path = tmp_path / "in.mp4"
    input_path.write_bytes(b"\x00" * 64)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    unavailable_detection = type("Det", (), {
        "ok": False,
        "usable": None,
        "reason": "detector_dependency_unavailable",
        "message": "mediapipe not installed",
        "frames_sampled": 0,
        "track_samples": None,
        "samples": [],
    })()
    unavailable_track = type("Track", (), {
        "ok": False,
        "usable": False,
        "reason": "detection_failed",
        "message": "detection failed",
        "frames_sampled": 0,
        "track_samples": 0,
        "samples": [],
    })()
    unavailable_crop = type("Crop", (), {
        "ok": False,
        "usable": False,
        "reason": "track_not_usable",
        "message": "track not usable",
        "frames_sampled": None,
        "track_samples": None,
        "samples": [],
        "smoothing_method": None,
        "movement_stats": None,
        "crop_width": 0,
        "crop_height": 0,
    })()

    with patch(
        "smoke_face_track_reframing.build_smoothed_face_crop_path_for_clip",
        return_value=(unavailable_detection, unavailable_track, unavailable_crop, unavailable_crop),
    ):
        artifacts = smoke.collect_pipeline_artifacts(
            input_path=input_path,
            output_dir=output_dir,
            tmp_dir=tmp_path / "tmp",
            source_width=640,
            source_height=360,
        )

    assert artifacts["ok"] is True
    assert artifacts["detection_ok"] is False
    assert (output_dir / "face_track_render_report.json").is_file()


def test_smoke_report_contains_visual_checklist():
    report = smoke.build_smoke_report(
        input_path=Path("/in.mp4"),
        output_dir=Path("/out"),
        input_info={"width": 640, "height": 360, "duration_sec": 2.0, "has_audio": True},
        modes=["blur_background"],
        mode_results={},
        comparison_manifest={},
        mediapipe_available=False,
        mediapipe_message="missing",
        detector_available=False,
        detector_message="missing",
        pipeline_artifacts=None,
    )
    assert len(report["visual_qa_checklist"]) == len(smoke.VISUAL_QA_CHECKLIST)
    assert report["production_default_reframe_mode"] == "blur_background"


def test_build_render_report_includes_renderer_optimization_fields():
    metadata = {
        "face_track_rendered": True,
        "segments_planned": 40,
        "segments_rendered": 18,
        "segments_merged": 22,
        "unique_crop_rects_before_merge": 15,
        "unique_crop_rects_after_merge": 12,
        "segment_crop_change_threshold_px": 4,
    }
    report = smoke.build_render_report_from_metadata(metadata)
    assert report["segments_planned"] == 40
    assert report["segments_merged"] == 22
    assert report["unique_crop_rects_after_merge"] == 12


def test_renderer_optimization_stats_from_metadata():
    stats = smoke.renderer_optimization_stats_from_metadata({"face_track_rendered": False})
    assert stats is None
    stats = smoke.renderer_optimization_stats_from_metadata(
        {"face_track_rendered": True, "segments_planned": 10, "segments_rendered": 4}
    )
    assert stats["segments_planned"] == 10
    assert stats["segments_rendered"] == 4


def test_eligibility_stats_from_metadata():
    stats = smoke.eligibility_stats_from_metadata(
        {
            "face_track_eligible": False,
            "face_track_eligibility_reason": "leading_no_face_gap",
            "face_coverage_pct": 92.5,
            "leading_no_face_gap_sec": 1.5,
            "trailing_no_face_gap_sec": 0.0,
            "longest_face_run_sec": 18.5,
            "longest_face_run_pct": 92.5,
            "crop_x_range_pct_of_source_width": 16.25,
            "max_adjacent_crop_x_jump_pct_of_crop_width": 12.0,
            "layout_risk": False,
        }
    )
    assert stats["face_track_eligibility_reason"] == "leading_no_face_gap"
    assert stats["leading_no_face_gap_sec"] == 1.5


def test_production_default_unchanged():
    assert DEFAULT_REFRAME_MODE == "blur_background"
