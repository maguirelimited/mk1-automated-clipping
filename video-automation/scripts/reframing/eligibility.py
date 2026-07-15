"""Face-track eligibility gating for platform_safe_format_v1."""

from __future__ import annotations

import json
import math
import os
from typing import Any

from reframing.tracker import (
    REASON_EXCESSIVE_GAP,
    REASON_INSUFFICIENT_TRACK_COVERAGE,
    REASON_NO_DETECTIONS,
    _group_detections_by_frame,
    _longest_missing_gap_sec,
    _primary_face_score,
)
from reframing.types import (
    CropPathReport,
    CropPathSample,
    DetectionReport,
    FaceTrackEligibilityReport,
    TrackReport,
)

# ---------------------------------------------------------------------------
# Defaults (internal — not exposed in Ops UI yet)
# ---------------------------------------------------------------------------

DEFAULT_MIN_FACE_COVERAGE_PCT = 70.0
DEFAULT_MAX_NO_FACE_GAP_SEC = 2.0
DEFAULT_MIN_LONGEST_FACE_RUN_SEC = 8.0
DEFAULT_MAX_LEADING_NO_FACE_SEC = 1.0
DEFAULT_MAX_TRAILING_NO_FACE_SEC = 2.0
DEFAULT_MIN_LONGEST_FACE_RUN_PCT = 75.0
DEFAULT_MIN_MULTI_FACE_FRAME_PCT = 25.0
DEFAULT_MIN_SPLIT_SCREEN_FRAME_PCT = 20.0
DEFAULT_MIN_PRIMARY_TRACK_DOMINANCE_PCT = 70.0
DEFAULT_MAX_CROP_X_RANGE_PCT_OF_SOURCE_WIDTH = 18.0
DEFAULT_MAX_ADJACENT_CROP_X_JUMP_PCT_OF_CROP_WIDTH = 25.0

REASON_DETECTOR_UNAVAILABLE = "detector_unavailable"
REASON_TRACK_NOT_USABLE = "track_not_usable"
REASON_NO_FACES = "no_faces"
REASON_INSUFFICIENT_FACE_COVERAGE = "insufficient_face_coverage"
REASON_LONG_NO_FACE_GAP = "long_no_face_gap"
REASON_LEADING_NO_FACE_GAP = "leading_no_face_gap"
REASON_TRAILING_NO_FACE_GAP = "trailing_no_face_gap"
REASON_NO_SUSTAINED_FACE_VISIBLE_RUN = "no_sustained_face_visible_run"
REASON_INSUFFICIENT_SUSTAINED_FACE_RUN_PCT = "insufficient_sustained_face_run_pct"
REASON_MULTI_FACE_LAYOUT_RISK = "multi_face_layout_risk"
REASON_UNSTABLE_CROP_LAYOUT = "unstable_crop_layout"
REASON_CROP_JUMP_RISK = "crop_jump_risk"
REASON_ELIGIBLE = "eligible"

ELIGIBILITY_FALLBACK_MODE = "blur_background"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def merge_eligibility_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge caller overrides with eligibility defaults."""
    merged = {
        "min_face_coverage_pct": DEFAULT_MIN_FACE_COVERAGE_PCT,
        "max_no_face_gap_sec": DEFAULT_MAX_NO_FACE_GAP_SEC,
        "min_longest_face_run_sec": DEFAULT_MIN_LONGEST_FACE_RUN_SEC,
        "max_leading_no_face_sec": DEFAULT_MAX_LEADING_NO_FACE_SEC,
        "max_trailing_no_face_sec": DEFAULT_MAX_TRAILING_NO_FACE_SEC,
        "min_longest_face_run_pct": DEFAULT_MIN_LONGEST_FACE_RUN_PCT,
        "min_multi_face_frame_pct": DEFAULT_MIN_MULTI_FACE_FRAME_PCT,
        "min_split_screen_frame_pct": DEFAULT_MIN_SPLIT_SCREEN_FRAME_PCT,
        "min_primary_track_dominance_pct": DEFAULT_MIN_PRIMARY_TRACK_DOMINANCE_PCT,
        "max_crop_x_range_pct_of_source_width": DEFAULT_MAX_CROP_X_RANGE_PCT_OF_SOURCE_WIDTH,
        "max_adjacent_crop_x_jump_pct_of_crop_width": (
            DEFAULT_MAX_ADJACENT_CROP_X_JUMP_PCT_OF_CROP_WIDTH
        ),
    }
    if config:
        for key in merged:
            if key in config:
                merged[key] = config[key]
    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_face_track_eligibility(
    detection_report: DetectionReport,
    track_report: TrackReport,
    *,
    crop_path_report: CropPathReport | None = None,
    smoothed_crop_path_report: CropPathReport | None = None,
    clip_duration_sec: float | None = None,
    config: dict[str, Any] | None = None,
) -> FaceTrackEligibilityReport:
    """Decide whether face-track crop is appropriate for this clip."""
    thresholds = merge_eligibility_config(config)
    crop_report = _select_crop_report_for_layout(
        smoothed_crop_path_report,
        crop_path_report,
    )
    metrics = _compute_eligibility_metrics(
        detection_report,
        track_report,
        crop_report=crop_report,
        clip_duration_sec=clip_duration_sec,
        thresholds=thresholds,
    )

    if not detection_report.ok:
        return _ineligible_report(
            reason=REASON_DETECTOR_UNAVAILABLE,
            message=detection_report.message or "Face detection is unavailable.",
            thresholds=thresholds,
            metrics=metrics,
            track_usable=track_report.usable,
        )

    if not track_report.ok:
        reason = _map_track_failure_reason(track_report.reason)
        return _ineligible_report(
            reason=reason,
            message=track_report.message or "Face tracking failed.",
            thresholds=thresholds,
            metrics=metrics,
            track_usable=False,
        )

    min_coverage = float(thresholds["min_face_coverage_pct"])
    if metrics["face_coverage_pct"] < min_coverage:
        return _ineligible_report(
            reason=REASON_INSUFFICIENT_FACE_COVERAGE,
            message=(
                f"Face coverage {metrics['face_coverage_pct']:.2f}% is below the "
                f"{min_coverage:.2f}% minimum."
            ),
            thresholds=thresholds,
            metrics=metrics,
            track_usable=track_report.usable,
        )

    max_gap = float(thresholds["max_no_face_gap_sec"])
    max_leading = float(thresholds["max_leading_no_face_sec"])
    if metrics["leading_no_face_gap_sec"] > max_leading:
        return _ineligible_report(
            reason=REASON_LEADING_NO_FACE_GAP,
            message=(
                f"Leading no-face gap {metrics['leading_no_face_gap_sec']:.3f}s exceeds "
                f"the {max_leading:.3f}s limit."
            ),
            thresholds=thresholds,
            metrics=metrics,
            track_usable=track_report.usable,
        )

    max_trailing = float(thresholds["max_trailing_no_face_sec"])
    if metrics["trailing_no_face_gap_sec"] > max_trailing:
        return _ineligible_report(
            reason=REASON_TRAILING_NO_FACE_GAP,
            message=(
                f"Trailing no-face gap {metrics['trailing_no_face_gap_sec']:.3f}s exceeds "
                f"the {max_trailing:.3f}s limit."
            ),
            thresholds=thresholds,
            metrics=metrics,
            track_usable=track_report.usable,
        )

    if metrics["max_no_face_gap_sec"] > max_gap:
        return _ineligible_report(
            reason=REASON_LONG_NO_FACE_GAP,
            message=(
                f"Longest no-face gap {metrics['max_no_face_gap_sec']:.3f}s exceeds "
                f"the {max_gap:.3f}s limit."
            ),
            thresholds=thresholds,
            metrics=metrics,
            track_usable=track_report.usable,
        )

    min_run = float(thresholds["min_longest_face_run_sec"])
    if metrics["longest_face_run_sec"] < min_run:
        return _ineligible_report(
            reason=REASON_NO_SUSTAINED_FACE_VISIBLE_RUN,
            message=(
                f"Longest continuous face-visible run "
                f"{metrics['longest_face_run_sec']:.3f}s is below the "
                f"{min_run:.3f}s minimum."
            ),
            thresholds=thresholds,
            metrics=metrics,
            track_usable=track_report.usable,
        )

    min_run_pct = float(thresholds["min_longest_face_run_pct"])
    if metrics["longest_face_run_pct"] < min_run_pct:
        return _ineligible_report(
            reason=REASON_INSUFFICIENT_SUSTAINED_FACE_RUN_PCT,
            message=(
                f"Longest face run {metrics['longest_face_run_pct']:.2f}% of clip duration "
                f"is below the {min_run_pct:.2f}% minimum."
            ),
            thresholds=thresholds,
            metrics=metrics,
            track_usable=track_report.usable,
        )

    if metrics["multi_face_risk"]:
        return _ineligible_report(
            reason=REASON_MULTI_FACE_LAYOUT_RISK,
            message=(
                "Multiple competing faces or split-screen layout detected; "
                "face-track crop is not suitable."
            ),
            thresholds=thresholds,
            metrics=metrics,
            track_usable=track_report.usable,
        )

    if crop_report is not None and metrics["crop_layout_evaluated"]:
        max_crop_range_pct = float(thresholds["max_crop_x_range_pct_of_source_width"])
        if metrics["crop_x_range_pct_of_source_width"] > max_crop_range_pct:
            return _ineligible_report(
                reason=REASON_UNSTABLE_CROP_LAYOUT,
                message=(
                    f"Crop horizontal travel {metrics['crop_x_range_pct_of_source_width']:.2f}% "
                    f"of source width exceeds the {max_crop_range_pct:.2f}% limit."
                ),
                thresholds=thresholds,
                metrics=metrics,
                track_usable=track_report.usable,
            )

        max_jump_pct = float(thresholds["max_adjacent_crop_x_jump_pct_of_crop_width"])
        if metrics["max_adjacent_crop_x_jump_pct_of_crop_width"] > max_jump_pct:
            return _ineligible_report(
                reason=REASON_CROP_JUMP_RISK,
                message=(
                    f"Largest adjacent crop jump "
                    f"{metrics['max_adjacent_crop_x_jump_pct_of_crop_width']:.2f}% of crop "
                    f"width exceeds the {max_jump_pct:.2f}% limit."
                ),
                thresholds=thresholds,
                metrics=metrics,
                track_usable=track_report.usable,
            )

    if not track_report.usable:
        reason = _map_track_failure_reason(track_report.reason)
        return _ineligible_report(
            reason=reason,
            message=track_report.message or "Primary face track is not usable.",
            thresholds=thresholds,
            metrics=metrics,
            track_usable=False,
        )

    return _eligible_report(thresholds=thresholds, metrics=metrics)


def face_track_eligibility_metadata(
    report: FaceTrackEligibilityReport | None,
    *,
    reframe_mode: str | None = None,
    use_blur_fallback: bool = False,
) -> dict[str, Any]:
    """Build safe metadata fields for platform_safe_format_v1."""
    if report is None:
        return {}

    metadata: dict[str, Any] = {
        "face_track_eligible": report.eligible,
        "face_track_eligibility_reason": report.reason,
        "face_coverage_pct": round(report.face_coverage_pct, 2),
        "max_no_face_gap_sec": round(report.max_no_face_gap_sec, 3),
        "longest_face_run_sec": round(report.longest_face_run_sec, 3),
        "leading_no_face_gap_sec": round(report.leading_no_face_gap_sec, 3),
        "trailing_no_face_gap_sec": round(report.trailing_no_face_gap_sec, 3),
        "longest_face_run_pct": round(report.longest_face_run_pct, 2),
        "crop_x_range_px": round(report.crop_x_range_px, 2),
        "crop_x_range_pct_of_source_width": round(report.crop_x_range_pct_of_source_width, 2),
        "max_adjacent_crop_x_jump_px": round(report.max_adjacent_crop_x_jump_px, 2),
        "max_adjacent_crop_x_jump_pct_of_crop_width": round(
            report.max_adjacent_crop_x_jump_pct_of_crop_width,
            2,
        ),
        "layout_risk": report.layout_risk,
        "multi_face_risk": report.multi_face_risk,
        "primary_track_dominance_pct": round(report.primary_track_dominance_pct, 2),
    }
    if report.eligible:
        return metadata

    if use_blur_fallback and reframe_mode == "auto":
        metadata["face_track_eligibility_fallback"] = ELIGIBILITY_FALLBACK_MODE
    return metadata


def write_eligibility_report(path: str, report: FaceTrackEligibilityReport) -> None:
    """Write a face-track eligibility sidecar JSON file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report.to_sidecar_dict(), fh, indent=2, sort_keys=True)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Metrics and helpers
# ---------------------------------------------------------------------------


def _select_crop_report_for_layout(
    smoothed_crop_path_report: CropPathReport | None,
    crop_path_report: CropPathReport | None,
) -> CropPathReport | None:
    if (
        smoothed_crop_path_report is not None
        and smoothed_crop_path_report.ok
        and smoothed_crop_path_report.usable
        and smoothed_crop_path_report.samples
    ):
        return smoothed_crop_path_report
    if (
        crop_path_report is not None
        and crop_path_report.ok
        and crop_path_report.usable
        and crop_path_report.samples
    ):
        return crop_path_report
    return None


def _compute_eligibility_metrics(
    detection_report: DetectionReport,
    track_report: TrackReport,
    *,
    crop_report: CropPathReport | None,
    clip_duration_sec: float | None,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    frames_sampled = detection_report.frames_sampled or getattr(track_report, "frames_sampled", 0) or 0
    frames_with_faces = getattr(detection_report, "frames_with_faces", 0) or 0
    if frames_with_faces <= 0 and getattr(detection_report, "detections", None):
        frames_with_faces = len(_group_detections_by_frame(detection_report.detections))

    face_coverage_pct = getattr(track_report, "track_coverage_pct", 0.0) or 0.0
    if face_coverage_pct <= 0 and frames_sampled > 0:
        face_coverage_pct = (frames_with_faces / frames_sampled) * 100.0

    detection_fps = float(getattr(detection_report, "detection_fps", 0.0) or 0.0)
    frame_step = 1.0 / detection_fps if detection_fps > 0 else 0.5

    max_no_face_gap_sec = getattr(track_report, "max_gap_sec", 0.0) or 0.0
    longest_face_run_sec = 0.0
    leading_no_face_gap_sec = 0.0
    trailing_no_face_gap_sec = 0.0
    track_samples = getattr(track_report, "track_samples", 0) or 0
    track = getattr(track_report, "track", None) or []
    if track:
        max_no_face_gap_sec = max(
            max_no_face_gap_sec,
            _longest_missing_gap_sec(track, frame_step=frame_step),
        )
        longest_face_run_sec = _longest_present_run_sec(track, frame_step=frame_step)
        leading_no_face_gap_sec = _leading_no_face_gap_sec(track, frame_step=frame_step)
        trailing_no_face_gap_sec = _trailing_no_face_gap_sec(track, frame_step=frame_step)
    elif track_samples > 0:
        longest_face_run_sec = track_samples * frame_step

    clip_duration = clip_duration_sec
    if clip_duration is None or clip_duration <= 0:
        clip_duration = frames_sampled * frame_step if frames_sampled > 0 else 0.0
    longest_face_run_pct = (
        (longest_face_run_sec / clip_duration) * 100.0 if clip_duration > 0 else 0.0
    )

    multi_face_metrics = _multi_face_metrics(detection_report)
    multi_face_risk = _evaluate_multi_face_risk(multi_face_metrics, thresholds=thresholds)
    crop_layout = _crop_layout_metrics(crop_report)
    layout_risk = bool(
        multi_face_risk
        or (
            crop_layout["crop_layout_evaluated"]
            and (
                crop_layout["crop_x_range_pct_of_source_width"]
                > float(thresholds["max_crop_x_range_pct_of_source_width"])
                or crop_layout["max_adjacent_crop_x_jump_pct_of_crop_width"]
                > float(thresholds["max_adjacent_crop_x_jump_pct_of_crop_width"])
            )
        )
    )

    return {
        "face_coverage_pct": round(face_coverage_pct, 2),
        "max_no_face_gap_sec": round(max_no_face_gap_sec, 3),
        "longest_face_run_sec": round(longest_face_run_sec, 3),
        "leading_no_face_gap_sec": round(leading_no_face_gap_sec, 3),
        "trailing_no_face_gap_sec": round(trailing_no_face_gap_sec, 3),
        "longest_face_run_pct": round(longest_face_run_pct, 2),
        "clip_duration_sec": round(clip_duration, 3) if clip_duration else 0.0,
        "frames_sampled": frames_sampled,
        "frames_with_faces": frames_with_faces,
        "track_samples": track_samples,
        "multi_face_frame_pct": round(multi_face_metrics["multi_face_frame_pct"], 2),
        "split_screen_frame_pct": round(multi_face_metrics["split_screen_frame_pct"], 2),
        "primary_track_dominance_pct": round(
            multi_face_metrics["primary_track_dominance_pct"],
            2,
        ),
        "multi_face_risk": multi_face_risk,
        **crop_layout,
        "layout_risk": layout_risk,
    }


def _crop_layout_metrics(crop_report: CropPathReport | None) -> dict[str, Any]:
    empty = {
        "crop_x_range_px": 0.0,
        "crop_x_range_pct_of_source_width": 0.0,
        "crop_center_x_range_px": 0.0,
        "max_adjacent_crop_x_jump_px": 0.0,
        "max_adjacent_crop_x_jump_pct_of_crop_width": 0.0,
        "crop_layout_evaluated": False,
    }
    if crop_report is None or not crop_report.samples:
        return empty

    samples = sorted(crop_report.samples, key=lambda sample: sample.timestamp_sec)
    source_width = crop_report.source_width
    if source_width <= 0:
        source_width = max(
            sample.crop.x + sample.crop.width for sample in samples
        )

    crop_width = crop_report.crop_width or samples[0].crop.width
    xs = [sample.crop.x for sample in samples]
    centers = [sample.crop.x + (sample.crop.width / 2.0) for sample in samples]
    crop_x_range_px = float(max(xs) - min(xs)) if xs else 0.0
    crop_center_x_range_px = float(max(centers) - min(centers)) if centers else 0.0

    max_adjacent_crop_x_jump_px = 0.0
    for previous, current in zip(samples, samples[1:]):
        jump = abs(current.crop.x - previous.crop.x)
        max_adjacent_crop_x_jump_px = max(max_adjacent_crop_x_jump_px, float(jump))

    crop_x_range_pct = (
        (crop_x_range_px / source_width) * 100.0 if source_width > 0 else 0.0
    )
    max_jump_pct = (
        (max_adjacent_crop_x_jump_px / crop_width) * 100.0 if crop_width > 0 else 0.0
    )

    return {
        "crop_x_range_px": round(crop_x_range_px, 2),
        "crop_x_range_pct_of_source_width": round(crop_x_range_pct, 2),
        "crop_center_x_range_px": round(crop_center_x_range_px, 2),
        "max_adjacent_crop_x_jump_px": round(max_adjacent_crop_x_jump_px, 2),
        "max_adjacent_crop_x_jump_pct_of_crop_width": round(max_jump_pct, 2),
        "crop_layout_evaluated": True,
    }


def _multi_face_metrics(detection_report: DetectionReport) -> dict[str, Any]:
    frame_w = getattr(detection_report, "frame_width", 0) or 0
    frame_h = getattr(detection_report, "frame_height", 0) or 0
    by_frame = _group_detections_by_frame(getattr(detection_report, "detections", None) or [])

    if not by_frame or frame_w <= 0 or frame_h <= 0:
        return {
            "multi_face_frame_pct": 0.0,
            "split_screen_frame_pct": 0.0,
            "primary_track_dominance_pct": 100.0,
        }

    frames_with_faces = len(by_frame)
    multi_face_frames = 0
    split_screen_frames = 0
    dominance_hits = 0
    dominance_total = 0

    for faces in by_frame.values():
        if len(faces) < 2:
            continue

        multi_face_frames += 1
        ranked = sorted(
            faces,
            key=lambda det: _primary_face_score(det, frame_w=frame_w, frame_h=frame_h),
            reverse=True,
        )
        primary = ranked[0]
        largest = max(faces, key=lambda det: det.bbox.width * det.bbox.height)
        dominance_total += 1
        if (
            primary.bbox.x == largest.bbox.x
            and primary.bbox.y == largest.bbox.y
            and primary.bbox.width == largest.bbox.width
            and primary.bbox.height == largest.bbox.height
        ) or _bbox_center_distance(primary.bbox, largest.bbox) <= max(frame_w, frame_h) * 0.12:
            dominance_hits += 1

        if _is_split_screen_layout(faces, frame_w=frame_w):
            split_screen_frames += 1

    multi_face_frame_pct = (multi_face_frames / frames_with_faces) * 100.0
    split_screen_frame_pct = 0.0
    if multi_face_frames > 0:
        split_screen_frame_pct = (split_screen_frames / multi_face_frames) * 100.0

    primary_track_dominance_pct = 100.0
    if dominance_total > 0:
        primary_track_dominance_pct = (dominance_hits / dominance_total) * 100.0

    return {
        "multi_face_frame_pct": multi_face_frame_pct,
        "split_screen_frame_pct": split_screen_frame_pct,
        "primary_track_dominance_pct": primary_track_dominance_pct,
    }


def _evaluate_multi_face_risk(
    metrics: dict[str, Any],
    *,
    thresholds: dict[str, Any],
) -> bool:
    if metrics["multi_face_frame_pct"] >= float(thresholds["min_multi_face_frame_pct"]):
        if metrics["split_screen_frame_pct"] >= float(thresholds["min_split_screen_frame_pct"]):
            return True
        if metrics["primary_track_dominance_pct"] < float(
            thresholds["min_primary_track_dominance_pct"]
        ):
            return True
    return False


def _is_split_screen_layout(faces: list[Any], *, frame_w: int) -> bool:
    if len(faces) < 2:
        return False
    centers = sorted(_bbox_center_x(face.bbox) for face in faces)
    spread = centers[-1] - centers[0]
    return spread >= frame_w * 0.35


def _leading_no_face_gap_sec(track: list[Any], *, frame_step: float) -> float:
    missing = 0
    for sample in track:
        if sample.missing:
            missing += 1
        else:
            break
    return missing * frame_step


def _trailing_no_face_gap_sec(track: list[Any], *, frame_step: float) -> float:
    missing = 0
    for sample in reversed(track):
        if sample.missing:
            missing += 1
        else:
            break
    return missing * frame_step


def _bbox_center_x(bbox: Any) -> float:
    return bbox.x + (bbox.width / 2.0)


def _bbox_center_distance(left: Any, right: Any) -> float:
    left_cx = left.x + (left.width / 2.0)
    left_cy = left.y + (left.height / 2.0)
    right_cx = right.x + (right.width / 2.0)
    right_cy = right.y + (right.height / 2.0)
    return math.hypot(left_cx - right_cx, left_cy - right_cy)


def _longest_present_run_sec(
    track: list[Any],
    *,
    frame_step: float,
) -> float:
    longest = 0
    current = 0
    for sample in track:
        if sample.missing:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest * frame_step


def _map_track_failure_reason(reason: str | None) -> str:
    if reason == REASON_NO_DETECTIONS:
        return REASON_NO_FACES
    if reason == REASON_INSUFFICIENT_TRACK_COVERAGE:
        return REASON_INSUFFICIENT_FACE_COVERAGE
    if reason == REASON_EXCESSIVE_GAP:
        return REASON_LONG_NO_FACE_GAP
    return reason or REASON_TRACK_NOT_USABLE


def _report_from_metrics(
    *,
    eligible: bool,
    reason: str,
    message: str | None,
    thresholds: dict[str, Any],
    metrics: dict[str, Any],
    track_usable: bool,
) -> FaceTrackEligibilityReport:
    return FaceTrackEligibilityReport(
        ok=True,
        eligible=eligible,
        reason=reason,
        message=message,
        face_coverage_pct=metrics["face_coverage_pct"],
        max_no_face_gap_sec=metrics["max_no_face_gap_sec"],
        longest_face_run_sec=metrics["longest_face_run_sec"],
        leading_no_face_gap_sec=metrics["leading_no_face_gap_sec"],
        trailing_no_face_gap_sec=metrics["trailing_no_face_gap_sec"],
        longest_face_run_pct=metrics["longest_face_run_pct"],
        crop_x_range_px=metrics["crop_x_range_px"],
        crop_x_range_pct_of_source_width=metrics["crop_x_range_pct_of_source_width"],
        crop_center_x_range_px=metrics["crop_center_x_range_px"],
        max_adjacent_crop_x_jump_px=metrics["max_adjacent_crop_x_jump_px"],
        max_adjacent_crop_x_jump_pct_of_crop_width=metrics[
            "max_adjacent_crop_x_jump_pct_of_crop_width"
        ],
        layout_risk=metrics["layout_risk"],
        track_usable=track_usable,
        multi_face_risk=metrics["multi_face_risk"],
        primary_track_dominance_pct=metrics["primary_track_dominance_pct"],
        frames_sampled=metrics["frames_sampled"],
        frames_with_faces=metrics["frames_with_faces"],
        track_samples=metrics["track_samples"],
        thresholds=thresholds,
        metrics=metrics,
    )


def _ineligible_report(
    *,
    reason: str,
    message: str,
    thresholds: dict[str, Any],
    metrics: dict[str, Any],
    track_usable: bool,
) -> FaceTrackEligibilityReport:
    return _report_from_metrics(
        eligible=False,
        reason=reason,
        message=message,
        thresholds=thresholds,
        metrics=dict(metrics),
        track_usable=track_usable,
    )


def _eligible_report(
    *,
    thresholds: dict[str, Any],
    metrics: dict[str, Any],
) -> FaceTrackEligibilityReport:
    return _report_from_metrics(
        eligible=True,
        reason=REASON_ELIGIBLE,
        message=None,
        thresholds=thresholds,
        metrics=dict(metrics),
        track_usable=True,
    )
