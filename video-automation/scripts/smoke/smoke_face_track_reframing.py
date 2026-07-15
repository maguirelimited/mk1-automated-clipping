#!/usr/bin/env python3
"""Smoke / visual QA harness for face-track reframing.

Runs platform_safe_format_v1 in blur_background, face_track, and auto modes
against a local clip, writes comparison outputs, pipeline sidecars, and a
machine-readable smoke report for manual review.

Does not change production defaults. Default reframe_mode remains blur_background.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from post_processing_modules import MODULE_STATUS_FAIL, MODULE_STATUS_PASS  # noqa: E402
from platform_safe_format_v1 import PlatformSafeFormatV1Module, _probe_video_info  # noqa: E402
from reframing.config import (  # noqa: E402
    DEFAULT_FACE_TRACK_TEST_ENABLED,
    DEFAULT_REFRAME_MODE,
    FACE_TRACK_SKIP_REASON_TEST_DISABLED,
    FORMAT_STRATEGY_BLURRED_BACKGROUND,
    FORMAT_STRATEGY_FACE_TRACK_CROP,
    REFRAME_MODES,
)
from reframing.detector import detector_backend_available, mediapipe_import_status  # noqa: E402
from reframing.eligibility import evaluate_face_track_eligibility, write_eligibility_report  # noqa: E402
from reframing.smoother import build_smoothed_face_crop_path_for_clip  # noqa: E402
from reframing.types import CropPathReport, DetectionReport, TrackReport  # noqa: E402

SMOKE_VERSION = "1.0"
DEFAULT_TARGET_WIDTH = 1080
DEFAULT_TARGET_HEIGHT = 1920
DEFAULT_DURATION_TOLERANCE_SEC = 0.25

VISUAL_QA_CHECKLIST = [
    "Speaker face is visible throughout most of the clip",
    "Crop does not cut off the speaker's head badly",
    "Crop does not drift away from the speaker",
    "Crop movement is not distractingly jittery",
    "Crop movement is not too slow to follow the speaker",
    "No blurred background is visible in face_track output",
    "Captions would still have reasonable space later",
    "Audio remains in sync",
    "Output feels more native to Shorts/Reels/TikTok than blur_background",
]

MODE_OUTPUT_NAMES = {
    "blur_background": "blur_background.mp4",
    "face_track": "face_track.mp4",
    "auto": "auto.mp4",
    "auto_test_disabled": "auto_test_disabled.mp4",
    "auto_test_enabled": "auto_test_enabled.mp4",
}

# Smoke-only virtual modes for auto test-matrix runs.
SMOKE_MODE_CHOICES = (*REFRAME_MODES, "auto_test_disabled", "auto_test_enabled")


class SmokeHarnessError(RuntimeError):
    """Blocking smoke harness error (missing input, unreadable media, etc.)."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run face-track reframing smoke / visual QA against a local clip.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to a local input clip (talking-head footage recommended).",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for comparison outputs, sidecars, and smoke_report.json.",
    )
    parser.add_argument(
        "--mode",
        action="append",
        choices=SMOKE_MODE_CHOICES,
        dest="modes",
        help="Reframe mode to run. Repeat for multiple modes. Defaults to all modes.",
    )
    parser.add_argument(
        "--job-id",
        default="face_track_smoke",
        help="Job ID used for module output naming.",
    )
    parser.add_argument(
        "--candidate-id",
        default="smoke_001",
        help="Candidate ID used for module output naming.",
    )
    parser.add_argument(
        "--duration-tolerance-sec",
        type=float,
        default=DEFAULT_DURATION_TOLERANCE_SEC,
        help="Allowed output vs input duration delta.",
    )
    parser.add_argument(
        "--face-track-test-enabled",
        action="store_true",
        help=(
            "When running auto mode, enable face-track test gating (auto only). "
            "Ignored for auto_test_disabled / auto_test_enabled virtual modes."
        ),
    )
    parser.add_argument(
        "--run-auto-test-matrix",
        action="store_true",
        help=(
            "When auto is requested, also run auto_test_disabled and auto_test_enabled "
            "virtual modes for production test-matrix comparison."
        ),
    )
    parser.add_argument(
        "--skip-pipeline-sidecars",
        action="store_true",
        help="Skip writing detection/tracking/crop sidecar JSON files.",
    )
    return parser.parse_args(argv)


def _resolve_smoke_modes(args: argparse.Namespace) -> list[str]:
    modes = list(args.modes or REFRAME_MODES)
    if getattr(args, "run_auto_test_matrix", False) and "auto" in modes:
        expanded: list[str] = []
        for mode in modes:
            if mode == "auto":
                expanded.extend(["auto_test_disabled", "auto_test_enabled"])
            else:
                expanded.append(mode)
        return expanded
    return modes


def resolve_smoke_mode_config(mode: str, args: argparse.Namespace) -> tuple[str, bool]:
    """Map smoke mode name to module reframe_mode and face_track_test_enabled."""
    if mode == "auto_test_disabled":
        return "auto", False
    if mode == "auto_test_enabled":
        return "auto", True
    if mode == "auto":
        return "auto", bool(getattr(args, "face_track_test_enabled", False))
    return mode, False


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the smoke harness and return the smoke report dict."""
    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    modes = _resolve_smoke_modes(args)

    if not input_path.is_file():
        raise SmokeHarnessError(f"input file does not exist: {input_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = output_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    input_info = probe_input_info(input_path)
    write_json(output_dir / "input_info.json", input_info)

    mediapipe_ok, mediapipe_message = mediapipe_import_status()
    detector_ok, detector_message = detector_backend_available("mediapipe")

    pipeline_artifacts = None
    if not args.skip_pipeline_sidecars:
        pipeline_artifacts = collect_pipeline_artifacts(
            input_path=input_path,
            output_dir=output_dir,
            tmp_dir=tmp_dir,
            source_width=int(input_info["width"]),
            source_height=int(input_info["height"]),
            clip_duration_sec=input_info.get("duration_sec"),
        )

    mode_results: dict[str, Any] = {}
    for mode in modes:
        reframe_mode, test_enabled = resolve_smoke_mode_config(mode, args)
        mode_results[mode] = run_mode_smoke(
            mode=mode,
            reframe_mode=reframe_mode,
            face_track_test_enabled=test_enabled,
            input_path=input_path,
            output_dir=output_dir,
            tmp_dir=tmp_dir,
            job_id=args.job_id,
            candidate_id=args.candidate_id,
            input_info=input_info,
            duration_tolerance_sec=float(args.duration_tolerance_sec),
        )

    comparison_manifest = {
        mode: str(output_dir / MODE_OUTPUT_NAMES[mode])
        for mode in modes
        if (output_dir / MODE_OUTPUT_NAMES[mode]).is_file()
    }

    smoke_report = build_smoke_report(
        input_path=input_path,
        output_dir=output_dir,
        input_info=input_info,
        modes=modes,
        mode_results=mode_results,
        comparison_manifest=comparison_manifest,
        mediapipe_available=mediapipe_ok,
        mediapipe_message=mediapipe_message,
        detector_available=detector_ok,
        detector_message=detector_message,
        pipeline_artifacts=pipeline_artifacts,
        default_face_track_test_enabled=DEFAULT_FACE_TRACK_TEST_ENABLED,
    )
    write_json(output_dir / "smoke_report.json", smoke_report)
    return smoke_report


def probe_input_info(input_path: Path) -> dict[str, Any]:
    info = _probe_video_info(str(input_path))
    if info is None or info.get("width", 0) <= 0:
        raise SmokeHarnessError(f"ffprobe could not read input video: {input_path}")
    return {
        "input_path": str(input_path),
        "width": info["width"],
        "height": info["height"],
        "duration_sec": info.get("duration_sec"),
        "has_audio": bool(info.get("has_audio")),
    }


def collect_pipeline_artifacts(
    *,
    input_path: Path,
    output_dir: Path,
    tmp_dir: Path,
    source_width: int,
    source_height: int,
    clip_duration_sec: float | None = None,
) -> dict[str, Any]:
    """Run the face pipeline once and write JSON sidecars for manual QA."""
    detection_path = output_dir / "face_track_detection_report.json"
    track_path = output_dir / "face_track_track_report.json"
    crop_path = output_dir / "face_track_crop_path_report.json"
    smoothed_path = output_dir / "face_track_smoothed_crop_path_report.json"

    try:
        detection_report, track_report, crop_report, smoothed_report = (
            build_smoothed_face_crop_path_for_clip(
                str(input_path),
                source_width=source_width,
                source_height=source_height,
                tmp_dir=str(tmp_dir / "pipeline"),
                detection_report_path=str(detection_path),
                track_report_path=str(track_path),
                crop_path_report_path=str(crop_path),
                smoothed_crop_path_report_path=str(smoothed_path),
            )
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "sidecar_paths": {
                "detection_report": str(detection_path) if detection_path.is_file() else None,
                "track_report": str(track_path) if track_path.is_file() else None,
                "crop_path_report": str(crop_path) if crop_path.is_file() else None,
                "smoothed_crop_path_report": (
                    str(smoothed_path) if smoothed_path.is_file() else None
                ),
            },
        }

    eligibility_path = output_dir / "face_track_eligibility_report.json"
    eligibility_report = evaluate_face_track_eligibility(
        detection_report,
        track_report,
        crop_path_report=crop_report,
        smoothed_crop_path_report=smoothed_report,
        clip_duration_sec=clip_duration_sec,
    )
    write_eligibility_report(str(eligibility_path), eligibility_report)

    render_report_path = output_dir / "face_track_render_report.json"
    render_report = {
        "ok": smoothed_report.ok and smoothed_report.usable,
        "source": "pipeline_only",
        "message": (
            "Render metadata is populated after a face_track or successful auto "
            "mode run. This sidecar captures pipeline readiness only."
        ),
        "smoothed_crop_path_usable": smoothed_report.usable,
        "crop_path_usable": crop_report.usable,
        "track_usable": track_report.usable,
        "detection_ok": detection_report.ok,
        "smoothing_method": smoothed_report.smoothing_method,
        "movement_stats": smoothed_report.movement_stats,
        "crop_width": smoothed_report.crop_width,
        "crop_height": smoothed_report.crop_height,
        "samples": len(smoothed_report.samples),
    }
    write_json(render_report_path, render_report)

    return {
        "ok": True,
        "detection_ok": detection_report.ok,
        "track_usable": track_report.usable,
        "crop_path_usable": crop_report.usable,
        "smoothed_crop_path_usable": smoothed_report.usable,
        "eligibility": eligibility_report.to_sidecar_dict(),
        "sidecar_paths": {
            "detection_report": str(detection_path),
            "track_report": str(track_path),
            "crop_path_report": str(crop_path),
            "smoothed_crop_path_report": str(smoothed_path),
            "eligibility_report": str(eligibility_path),
            "render_report": str(render_report_path),
        },
        "summaries": {
            "detection": _report_summary(detection_report),
            "track": _report_summary(track_report),
            "crop": _report_summary(crop_report),
            "smoothed": _report_summary(smoothed_report),
        },
    }


def run_mode_smoke(
    *,
    mode: str,
    reframe_mode: str,
    face_track_test_enabled: bool,
    input_path: Path,
    output_dir: Path,
    tmp_dir: Path,
    job_id: str,
    candidate_id: str,
    input_info: dict[str, Any],
    duration_tolerance_sec: float,
) -> dict[str, Any]:
    mode_tmp = tmp_dir / mode
    mode_tmp.mkdir(parents=True, exist_ok=True)

    context = {
        "job_id": job_id,
        "clip_dir": str(mode_tmp),
        "tmp_dir": str(mode_tmp / "work"),
        "selected_candidate": {"candidate_id": candidate_id},
        "config": {
            "reframe_mode": reframe_mode,
            "face_track_test_enabled": face_track_test_enabled,
            "overwrite": True,
            "target_width": DEFAULT_TARGET_WIDTH,
            "target_height": DEFAULT_TARGET_HEIGHT,
            "duration_tolerance_sec": max(duration_tolerance_sec, 0.25),
        },
    }

    result = PlatformSafeFormatV1Module().run(context, input_path=str(input_path))
    checks = validate_mode_output(
        mode=mode,
        reframe_mode=reframe_mode,
        face_track_test_enabled=face_track_test_enabled,
        result=result,
        input_info=input_info,
        duration_tolerance_sec=duration_tolerance_sec,
        target_width=DEFAULT_TARGET_WIDTH,
        target_height=DEFAULT_TARGET_HEIGHT,
    )

    named_output = output_dir / MODE_OUTPUT_NAMES[mode]
    module_output = result.get("output_path")
    if result.get("status") == MODULE_STATUS_PASS and module_output and Path(module_output).is_file():
        shutil.copy2(module_output, named_output)
        checks["named_output_path"] = str(named_output)
        if mode in ("face_track", "auto") and result.get("metadata", {}).get("face_track_rendered"):
            render_report = build_render_report_from_metadata(result.get("metadata") or {})
            write_json(output_dir / "face_track_render_report.json", render_report)
    else:
        checks["named_output_path"] = None

    return {
        "mode": mode,
        "status": result.get("status"),
        "output_path": result.get("output_path"),
        "named_output_path": str(named_output) if named_output.is_file() else None,
        "error_reason": result.get("error_reason"),
        "warnings": list(result.get("warnings") or []),
        "metadata": dict(result.get("metadata") or {}),
        "checks": checks,
    }


def validate_mode_output(
    *,
    mode: str,
    reframe_mode: str,
    face_track_test_enabled: bool,
    result: dict[str, Any],
    input_info: dict[str, Any],
    duration_tolerance_sec: float,
    target_width: int,
    target_height: int,
) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "module_status_ok": result.get("status") == MODULE_STATUS_PASS,
        "expected_strict_failure": False,
        "file_exists": False,
        "probe_ok": False,
        "dimensions_ok": False,
        "duration_ok": False,
        "audio_ok": False,
        "format_strategy_ok": False,
        "format_strategy": None,
        "face_track_test_enabled": face_track_test_enabled,
        "face_track_attempted": None,
        "face_track_eligible": None,
        "face_track_used": None,
        "fallback_reason": None,
        "issues": [],
    }

    metadata = result.get("metadata") or {}
    format_strategy = metadata.get("format_strategy")
    checks["format_strategy"] = format_strategy
    checks["face_track_attempted"] = metadata.get("face_track_attempted")
    checks["face_track_eligible"] = metadata.get("face_track_eligible")
    checks["face_track_used"] = metadata.get("face_track_used")
    checks["fallback_reason"] = metadata.get("face_track_skip_reason") or metadata.get(
        "face_track_eligibility_reason"
    )

    if mode == "blur_background":
        checks["expected_format_strategy"] = FORMAT_STRATEGY_BLURRED_BACKGROUND
        checks["format_strategy_ok"] = format_strategy == FORMAT_STRATEGY_BLURRED_BACKGROUND
        if metadata.get("reframe_attempted"):
            checks["issues"].append("blur_background should not set reframe_attempted=true")
        if metadata.get("face_detection_attempted"):
            checks["issues"].append("blur_background should not run face detection")

    elif mode == "face_track":
        if result.get("status") == MODULE_STATUS_FAIL:
            checks["expected_strict_failure"] = True
            checks["module_status_ok"] = True
            checks["format_strategy_ok"] = format_strategy != FORMAT_STRATEGY_FACE_TRACK_CROP
            if metadata.get("failure_code"):
                checks["failure_code"] = metadata["failure_code"]
            return checks
        checks["expected_format_strategy"] = FORMAT_STRATEGY_FACE_TRACK_CROP
        checks["format_strategy_ok"] = format_strategy == FORMAT_STRATEGY_FACE_TRACK_CROP
        if not metadata.get("face_track_rendered"):
            checks["issues"].append("face_track pass without face_track_rendered metadata")

    elif mode in ("auto", "auto_test_disabled", "auto_test_enabled"):
        if mode == "auto_test_disabled" or (mode == "auto" and not face_track_test_enabled):
            checks["expected_format_strategy"] = FORMAT_STRATEGY_BLURRED_BACKGROUND
            checks["format_strategy_ok"] = format_strategy == FORMAT_STRATEGY_BLURRED_BACKGROUND
            if metadata.get("face_track_attempted"):
                checks["issues"].append("auto with test disabled should not run face pipeline")
            if metadata.get("face_track_skip_reason") != FACE_TRACK_SKIP_REASON_TEST_DISABLED:
                if mode == "auto_test_disabled":
                    checks["issues"].append(
                        "auto_test_disabled should set face_track_skip_reason=test_disabled"
                    )
        elif format_strategy == FORMAT_STRATEGY_FACE_TRACK_CROP:
            checks["expected_format_strategy"] = FORMAT_STRATEGY_FACE_TRACK_CROP
            checks["format_strategy_ok"] = bool(metadata.get("face_track_rendered"))
        else:
            checks["expected_format_strategy"] = FORMAT_STRATEGY_BLURRED_BACKGROUND
            checks["format_strategy_ok"] = format_strategy == FORMAT_STRATEGY_BLURRED_BACKGROUND
            if (
                result.get("status") == MODULE_STATUS_PASS
                and not result.get("warnings")
                and face_track_test_enabled
            ):
                checks["issues"].append("auto blur fallback should usually include a warning")

    output_path = result.get("output_path")
    if result.get("status") != MODULE_STATUS_PASS or not output_path:
        if not checks["expected_strict_failure"]:
            checks["issues"].append("expected PASS output but module did not pass")
        return checks

    path = Path(output_path)
    checks["file_exists"] = path.is_file() and path.stat().st_size > 0
    if not checks["file_exists"]:
        checks["issues"].append("output file missing or empty")
        return checks

    probe = _probe_video_info(str(path))
    if probe is None or probe.get("width", 0) <= 0:
        checks["issues"].append("ffprobe could not read output")
        return checks

    checks["probe_ok"] = True
    checks["output_width"] = probe["width"]
    checks["output_height"] = probe["height"]
    checks["output_duration_sec"] = probe.get("duration_sec")
    checks["output_has_audio"] = bool(probe.get("has_audio"))

    checks["dimensions_ok"] = (
        probe["width"] == target_width and probe["height"] == target_height
    )
    if not checks["dimensions_ok"]:
        checks["issues"].append(
            f"output dimensions {probe['width']}x{probe['height']} != "
            f"{target_width}x{target_height}"
        )

    input_duration = input_info.get("duration_sec")
    output_duration = probe.get("duration_sec")
    if input_duration is not None and output_duration is not None:
        delta = abs(float(output_duration) - float(input_duration))
        checks["duration_delta_sec"] = round(delta, 3)
        checks["duration_ok"] = delta <= duration_tolerance_sec
        if not checks["duration_ok"]:
            checks["issues"].append(
                f"duration delta {delta:.3f}s exceeds tolerance {duration_tolerance_sec:.3f}s"
            )
    else:
        checks["duration_ok"] = True

    if input_info.get("has_audio"):
        checks["audio_ok"] = bool(probe.get("has_audio"))
        if not checks["audio_ok"]:
            checks["issues"].append("input had audio but output does not")
    else:
        checks["audio_ok"] = True

    return checks


def build_smoke_report(
    *,
    input_path: Path,
    output_dir: Path,
    input_info: dict[str, Any],
    modes: list[str],
    mode_results: dict[str, Any],
    comparison_manifest: dict[str, str],
    mediapipe_available: bool,
    mediapipe_message: str | None,
    detector_available: bool,
    detector_message: str | None,
    pipeline_artifacts: dict[str, Any] | None,
    default_face_track_test_enabled: bool = DEFAULT_FACE_TRACK_TEST_ENABLED,
) -> dict[str, Any]:
    return {
        "schema_version": "face_track_reframing_smoke_v1",
        "smoke_version": SMOKE_VERSION,
        "production_default_reframe_mode": DEFAULT_REFRAME_MODE,
        "default_face_track_test_enabled": default_face_track_test_enabled,
        "input_path": str(input_path),
        "output_dir": str(output_dir),
        "input_info": input_info,
        "modes_requested": modes,
        "comparison_manifest": comparison_manifest,
        "dependency_status": {
            "ffmpeg_available": shutil.which("ffmpeg") is not None,
            "ffprobe_available": shutil.which("ffprobe") is not None,
            "mediapipe_available": mediapipe_available,
            "mediapipe_message": mediapipe_message,
            "detector_available": detector_available,
            "detector_message": detector_message,
        },
        "pipeline_artifacts": pipeline_artifacts,
        "mode_results": mode_results,
        "visual_qa_checklist": [
            {"item": item, "checked": False} for item in VISUAL_QA_CHECKLIST
        ],
        "summary": summarize_mode_results(mode_results),
    }


def summarize_mode_results(mode_results: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for mode, payload in mode_results.items():
        checks = payload.get("checks") or {}
        summary[mode] = {
            "status": payload.get("status"),
            "format_strategy": checks.get("format_strategy"),
            "face_track_test_enabled": checks.get("face_track_test_enabled"),
            "face_track_attempted": checks.get("face_track_attempted"),
            "face_track_eligible": checks.get("face_track_eligible"),
            "face_track_used": checks.get("face_track_used"),
            "fallback_reason": checks.get("fallback_reason"),
            "named_output_path": payload.get("named_output_path"),
            "checks_passed": _checks_passed(checks),
            "issues": list(checks.get("issues") or []),
            "warnings": list(payload.get("warnings") or []),
            "failure_code": checks.get("failure_code"),
            "expected_strict_failure": checks.get("expected_strict_failure", False),
            "renderer_optimization": renderer_optimization_stats_from_metadata(
                payload.get("metadata") or {}
            ),
            "eligibility": eligibility_stats_from_metadata(payload.get("metadata") or {}),
        }
    return summary


def eligibility_stats_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    if "face_track_eligible" not in metadata:
        return None
    return {
        "face_track_eligible": metadata.get("face_track_eligible"),
        "face_track_eligibility_reason": metadata.get("face_track_eligibility_reason"),
        "face_coverage_pct": metadata.get("face_coverage_pct"),
        "leading_no_face_gap_sec": metadata.get("leading_no_face_gap_sec"),
        "trailing_no_face_gap_sec": metadata.get("trailing_no_face_gap_sec"),
        "longest_face_run_sec": metadata.get("longest_face_run_sec"),
        "longest_face_run_pct": metadata.get("longest_face_run_pct"),
        "max_no_face_gap_sec": metadata.get("max_no_face_gap_sec"),
        "crop_x_range_pct_of_source_width": metadata.get("crop_x_range_pct_of_source_width"),
        "max_adjacent_crop_x_jump_pct_of_crop_width": metadata.get(
            "max_adjacent_crop_x_jump_pct_of_crop_width"
        ),
        "layout_risk": metadata.get("layout_risk"),
        "multi_face_risk": metadata.get("multi_face_risk"),
    }


def build_render_report_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(metadata.get("face_track_rendered")),
        "source": "platform_safe_format_v1",
        "format_strategy": metadata.get("format_strategy"),
        "face_track_render_attempted": metadata.get("face_track_render_attempted"),
        "face_track_rendered": metadata.get("face_track_rendered"),
        "crop_renderer": metadata.get("crop_renderer"),
        "segments_planned": metadata.get("segments_planned"),
        "segments_rendered": metadata.get("segments_rendered"),
        "segments_merged": metadata.get("segments_merged"),
        "unique_crop_rects_before_merge": metadata.get("unique_crop_rects_before_merge"),
        "unique_crop_rects_after_merge": metadata.get("unique_crop_rects_after_merge"),
        "segment_crop_change_threshold_px": metadata.get("segment_crop_change_threshold_px"),
        "face_track_eligible": metadata.get("face_track_eligible"),
        "face_track_eligibility_reason": metadata.get("face_track_eligibility_reason"),
        "face_track_eligibility_fallback": metadata.get("face_track_eligibility_fallback"),
        "target_width": metadata.get("target_width"),
        "target_height": metadata.get("target_height"),
        "smoothing_method": metadata.get("smoothing_method"),
        "raw_total_movement_px": metadata.get("raw_total_movement_px"),
        "smoothed_total_movement_px": metadata.get("smoothed_total_movement_px"),
        "reframe_mode": metadata.get("reframe_mode"),
        "warnings": metadata.get("warnings"),
    }


def renderer_optimization_stats_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """Extract renderer optimisation stats when face-track render succeeded."""
    if not metadata.get("face_track_rendered"):
        return None
    return {
        "segments_planned": metadata.get("segments_planned"),
        "segments_rendered": metadata.get("segments_rendered"),
        "segments_merged": metadata.get("segments_merged"),
        "unique_crop_rects_before_merge": metadata.get("unique_crop_rects_before_merge"),
        "unique_crop_rects_after_merge": metadata.get("unique_crop_rects_after_merge"),
        "segment_crop_change_threshold_px": metadata.get("segment_crop_change_threshold_px"),
    }


def _checks_passed(checks: dict[str, Any]) -> bool:
    if checks.get("expected_strict_failure"):
        return bool(checks.get("module_status_ok")) and not checks.get("issues")
    required = (
        "module_status_ok",
        "file_exists",
        "probe_ok",
        "dimensions_ok",
        "duration_ok",
        "audio_ok",
        "format_strategy_ok",
    )
    return all(checks.get(key) for key in required) and not checks.get("issues")


def _report_summary(report: DetectionReport | TrackReport | CropPathReport) -> dict[str, Any]:
    payload = {
        "ok": getattr(report, "ok", None),
        "usable": getattr(report, "usable", None),
        "reason": getattr(report, "reason", None),
        "message": getattr(report, "message", None),
    }
    if hasattr(report, "frames_sampled"):
        payload["frames_sampled"] = report.frames_sampled
    if hasattr(report, "track_samples"):
        payload["track_samples"] = report.track_samples
    if hasattr(report, "samples"):
        payload["samples"] = len(report.samples)
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def print_visual_checklist(smoke_report: dict[str, Any]) -> None:
    print("\n== Visual QA checklist (manual) ==")
    for entry in smoke_report.get("visual_qa_checklist", []):
        print(f"[ ] {entry['item']}")
    print("\nCompare outputs:")
    for mode, path in (smoke_report.get("comparison_manifest") or {}).items():
        print(f"  {mode}: {path}")
    print(f"\nProduction default reframe_mode remains: {DEFAULT_REFRAME_MODE}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        smoke_report = run_smoke(args)
    except SmokeHarnessError as exc:
        print(f"FACE_TRACK_REFRAMING_SMOKE_FAILED: {exc}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as exc:
        print(f"FACE_TRACK_REFRAMING_SMOKE_FAILED: timed out: {exc}", file=sys.stderr)
        return 1

    print("FACE_TRACK_REFRAMING_SMOKE_COMPLETE")
    print(f"output_dir: {smoke_report['output_dir']}")
    print(f"production_default_reframe_mode: {smoke_report['production_default_reframe_mode']}")
    print(f"mediapipe_available: {smoke_report['dependency_status']['mediapipe_available']}")
    for mode, summary in smoke_report.get("summary", {}).items():
        print(
            f"{mode}: status={summary.get('status')} "
            f"format_strategy={summary.get('format_strategy')} "
            f"checks_passed={summary.get('checks_passed')}"
        )
        renderer_stats = summary.get("renderer_optimization")
        if renderer_stats:
            print(
                f"  renderer: segments_planned={renderer_stats.get('segments_planned')} "
                f"segments_rendered={renderer_stats.get('segments_rendered')} "
                f"segments_merged={renderer_stats.get('segments_merged')} "
                f"unique_crop_rects_before_merge="
                f"{renderer_stats.get('unique_crop_rects_before_merge')} "
                f"unique_crop_rects_after_merge="
                f"{renderer_stats.get('unique_crop_rects_after_merge')} "
                f"segment_crop_change_threshold_px="
                f"{renderer_stats.get('segment_crop_change_threshold_px')}"
            )
        eligibility_stats = summary.get("eligibility")
        if eligibility_stats:
            print(
                f"  eligibility: eligible={eligibility_stats.get('face_track_eligible')} "
                f"reason={eligibility_stats.get('face_track_eligibility_reason')} "
                f"face_coverage_pct={eligibility_stats.get('face_coverage_pct')} "
                f"leading_no_face_gap_sec={eligibility_stats.get('leading_no_face_gap_sec')} "
                f"trailing_no_face_gap_sec={eligibility_stats.get('trailing_no_face_gap_sec')} "
                f"longest_face_run_sec={eligibility_stats.get('longest_face_run_sec')} "
                f"longest_face_run_pct={eligibility_stats.get('longest_face_run_pct')} "
                f"crop_x_range_pct_of_source_width="
                f"{eligibility_stats.get('crop_x_range_pct_of_source_width')} "
                f"max_adjacent_crop_x_jump_pct_of_crop_width="
                f"{eligibility_stats.get('max_adjacent_crop_x_jump_pct_of_crop_width')} "
                f"layout_risk={eligibility_stats.get('layout_risk')}"
            )
    print_visual_checklist(smoke_report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
