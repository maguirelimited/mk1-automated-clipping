"""Single-speaker face tracking for future face-track crop reframing."""

from __future__ import annotations

import json
import math
import os
from typing import Any

from reframing.detector import detect_faces_for_clip, merge_detector_config, write_detection_report
from reframing.types import (
    BoundingBox,
    DetectionReport,
    FaceDetection,
    TrackReport,
    TrackedFaceSample,
)

# ---------------------------------------------------------------------------
# Defaults (internal — not exposed in Ops UI yet)
# ---------------------------------------------------------------------------

DEFAULT_MIN_TRACK_SAMPLES = 3
DEFAULT_MIN_TRACK_COVERAGE_PCT = 20.0
DEFAULT_MAX_GAP_SEC = 1.5

PRIMARY_SELECTION_METHOD = "largest_confident_centre_weighted"

CONFIDENCE_WEIGHT = 0.4
SIZE_WEIGHT = 0.4
CENTRE_WEIGHT = 0.2

MAX_JUMP_RATIO = 0.4

REASON_NO_DETECTIONS = "no_detections"
REASON_DETECTION_FAILED = "detection_failed"
REASON_INSUFFICIENT_TRACK_COVERAGE = "insufficient_track_coverage"
REASON_INSUFFICIENT_TRACK_SAMPLES = "insufficient_track_samples"
REASON_EXCESSIVE_GAP = "excessive_gap"
REASON_INVALID_FRAME_DIMENSIONS = "invalid_frame_dimensions"
REASON_INVALID_BBOX = "invalid_bbox"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def merge_tracker_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge caller overrides with tracker defaults."""
    merged = merge_detector_config(config)
    merged.update(
        {
            "min_track_samples": DEFAULT_MIN_TRACK_SAMPLES,
            "min_track_coverage_pct": DEFAULT_MIN_TRACK_COVERAGE_PCT,
            "max_gap_sec": DEFAULT_MAX_GAP_SEC,
        }
    )
    if config:
        merged.update(config)
    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_primary_face_track(
    detection_report: DetectionReport,
    *,
    config: dict[str, Any] | None = None,
) -> TrackReport:
    """Build one deterministic primary-speaker track from a detection report."""
    merged = merge_tracker_config(config)
    min_track_samples = int(merged["min_track_samples"])
    min_track_coverage_pct = float(merged["min_track_coverage_pct"])
    max_gap_sec = float(merged["max_gap_sec"])
    detection_fps = float(detection_report.detection_fps or 0.0)

    if not detection_report.ok:
        return _failure_track_report(
            input_path=detection_report.input_path,
            reason=REASON_DETECTION_FAILED,
            message=detection_report.message or "Face detection failed.",
            frames_sampled=detection_report.frames_sampled,
        )

    if detection_report.frames_sampled <= 0:
        return _failure_track_report(
            input_path=detection_report.input_path,
            reason=REASON_NO_DETECTIONS,
            message="No sampled frames were available for tracking.",
        )

    if not detection_report.detections:
        return _failure_track_report(
            input_path=detection_report.input_path,
            reason=REASON_NO_DETECTIONS,
            message="No face detections were available for tracking.",
            frames_sampled=detection_report.frames_sampled,
        )

    frame_w = detection_report.frame_width
    frame_h = detection_report.frame_height
    if not frame_w or not frame_h or frame_w <= 0 or frame_h <= 0:
        return _failure_track_report(
            input_path=detection_report.input_path,
            reason=REASON_INVALID_FRAME_DIMENSIONS,
            message="Detection report is missing valid frame dimensions.",
            frames_sampled=detection_report.frames_sampled,
        )

    valid_detections = [
        det for det in detection_report.detections if _is_valid_detection(det, frame_w, frame_h)
    ]
    if not valid_detections:
        return _failure_track_report(
            input_path=detection_report.input_path,
            reason=REASON_INVALID_BBOX,
            message="All face detections had invalid bounding boxes.",
            frames_sampled=detection_report.frames_sampled,
        )

    by_frame = _group_detections_by_frame(valid_detections)
    frame_step = 1.0 / detection_fps if detection_fps > 0 else 0.5
    track = _associate_primary_track(
        frames_sampled=detection_report.frames_sampled,
        by_frame=by_frame,
        frame_w=frame_w,
        frame_h=frame_h,
        frame_step=frame_step,
    )

    observed_samples = sum(1 for sample in track if not sample.missing)
    coverage_pct = (observed_samples / detection_report.frames_sampled) * 100.0
    longest_gap_sec = _longest_missing_gap_sec(track, frame_step=frame_step)

    usable, reason, message = _evaluate_track_usability(
        observed_samples=observed_samples,
        coverage_pct=coverage_pct,
        longest_gap_sec=longest_gap_sec,
        min_track_samples=min_track_samples,
        min_track_coverage_pct=min_track_coverage_pct,
        max_gap_sec=max_gap_sec,
    )

    return TrackReport(
        ok=True,
        usable=usable,
        input_path=detection_report.input_path,
        frames_sampled=detection_report.frames_sampled,
        track_samples=observed_samples,
        track_coverage_pct=coverage_pct,
        max_gap_sec=longest_gap_sec,
        primary_selection=PRIMARY_SELECTION_METHOD,
        track=track,
        reason=None if usable else reason,
        message=None if usable else message,
    )


def build_face_track_for_clip(
    input_path: str,
    *,
    tmp_dir: str,
    config: dict[str, Any] | None = None,
    detection_report_path: str | None = None,
    track_report_path: str | None = None,
) -> tuple[DetectionReport, TrackReport]:
    """Run detection and build a primary face track for one clip."""
    detection_report = detect_faces_for_clip(
        input_path,
        tmp_dir=tmp_dir,
        config=config,
        report_path=detection_report_path,
    )
    track_report = build_primary_face_track(detection_report, config=config)
    if track_report_path:
        write_track_report(track_report_path, track_report)
    return detection_report, track_report


def write_track_report(path: str, report: TrackReport) -> None:
    """Write a track sidecar JSON file for debugging."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2, sort_keys=True)
        fh.write("\n")


def face_pipeline_metadata(
    *,
    detection_report: DetectionReport | None,
    track_report: TrackReport | None,
    reframe_attempted: bool = True,
) -> dict[str, Any]:
    """Build safe metadata fields for platform_safe_format_v1."""
    metadata: dict[str, Any] = {
        "reframe_attempted": reframe_attempted,
        "face_detection_attempted": detection_report is not None,
        "face_tracking_attempted": track_report is not None,
    }
    if detection_report is not None:
        metadata["face_detection_ok"] = detection_report.ok
        if detection_report.reason:
            metadata["face_detection_reason"] = detection_report.reason
    if track_report is not None:
        metadata["face_track_ok"] = track_report.ok
        metadata["face_track_usable"] = track_report.usable
        metadata["face_track_coverage_pct"] = round(track_report.track_coverage_pct, 2)
        metadata["face_track_samples"] = track_report.track_samples
        metadata["face_track_max_gap_sec"] = round(track_report.max_gap_sec, 3)
        if track_report.reason:
            metadata["face_track_reason"] = track_report.reason
    return metadata


# ---------------------------------------------------------------------------
# Primary-face scoring and association
# ---------------------------------------------------------------------------


def _primary_face_score(detection: FaceDetection, *, frame_w: int, frame_h: int) -> float:
    cx, cy = _bbox_center(detection.bbox)
    area_norm = (detection.bbox.width * detection.bbox.height) / float(frame_w * frame_h)
    frame_cx = frame_w / 2.0
    frame_cy = frame_h / 2.0
    max_dist = math.hypot(frame_cx, frame_cy) or 1.0
    centre_bias = 1.0 - (math.hypot(cx - frame_cx, cy - frame_cy) / max_dist)
    return (
        CONFIDENCE_WEIGHT * detection.confidence
        + SIZE_WEIGHT * area_norm
        + CENTRE_WEIGHT * centre_bias
    )


def _associate_primary_track(
    *,
    frames_sampled: int,
    by_frame: dict[int, list[FaceDetection]],
    frame_w: int,
    frame_h: int,
    frame_step: float,
) -> list[TrackedFaceSample]:
    seed_frame = min(by_frame)
    seed_detection = max(
        by_frame[seed_frame],
        key=lambda det: _primary_face_score(det, frame_w=frame_w, frame_h=frame_h),
    )

    prev_center = _bbox_center(seed_detection.bbox)
    prev_bbox = seed_detection.bbox
    prev_confidence = seed_detection.confidence
    max_jump = MAX_JUMP_RATIO * math.hypot(frame_w, frame_h)

    track: list[TrackedFaceSample] = []
    for frame_index in range(frames_sampled):
        timestamp_sec = frame_index * frame_step
        faces = by_frame.get(frame_index, [])

        if not faces:
            track.append(
                TrackedFaceSample(
                    timestamp_sec=timestamp_sec,
                    frame_index=frame_index,
                    bbox=prev_bbox,
                    confidence=prev_confidence,
                    missing=True,
                    held=True,
                )
            )
            continue

        chosen = _choose_closest_face(
            faces,
            prev_center=prev_center,
            frame_w=frame_w,
            frame_h=frame_h,
            max_jump=max_jump,
        )
        prev_center = _bbox_center(chosen.bbox)
        prev_bbox = chosen.bbox
        prev_confidence = chosen.confidence
        track.append(
            TrackedFaceSample(
                timestamp_sec=timestamp_sec,
                frame_index=frame_index,
                bbox=chosen.bbox,
                confidence=chosen.confidence,
                missing=False,
                held=False,
            )
        )

    return track


def _choose_closest_face(
    faces: list[FaceDetection],
    *,
    prev_center: tuple[float, float],
    frame_w: int,
    frame_h: int,
    max_jump: float,
) -> FaceDetection:
    scored: list[tuple[float, FaceDetection]] = []
    for face in faces:
        cx, cy = _bbox_center(face.bbox)
        dist = math.hypot(cx - prev_center[0], cy - prev_center[1])
        scored.append((dist, face))

    scored.sort(key=lambda item: item[0])
    closest_dist, closest_face = scored[0]

    if closest_dist <= max_jump or len(scored) == 1:
        return closest_face

    return max(faces, key=lambda det: _primary_face_score(det, frame_w=frame_w, frame_h=frame_h))


# ---------------------------------------------------------------------------
# Usability and helpers
# ---------------------------------------------------------------------------


def _evaluate_track_usability(
    *,
    observed_samples: int,
    coverage_pct: float,
    longest_gap_sec: float,
    min_track_samples: int,
    min_track_coverage_pct: float,
    max_gap_sec: float,
) -> tuple[bool, str | None, str | None]:
    if observed_samples < min_track_samples:
        return (
            False,
            REASON_INSUFFICIENT_TRACK_SAMPLES,
            (
                f"Only {observed_samples} observed track samples; "
                f"minimum is {min_track_samples}."
            ),
        )

    if coverage_pct < min_track_coverage_pct:
        return (
            False,
            REASON_INSUFFICIENT_TRACK_COVERAGE,
            (
                f"Track coverage {coverage_pct:.1f}% is below the "
                f"{min_track_coverage_pct:.1f}% minimum."
            ),
        )

    if longest_gap_sec > max_gap_sec:
        return (
            False,
            REASON_EXCESSIVE_GAP,
            (
                f"Longest missing-face gap {longest_gap_sec:.3f}s exceeds "
                f"the {max_gap_sec:.3f}s limit."
            ),
        )

    return True, None, None


def _longest_missing_gap_sec(
    track: list[TrackedFaceSample],
    *,
    frame_step: float,
) -> float:
    longest = 0
    current = 0
    for sample in track:
        if sample.missing:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest * frame_step


def _group_detections_by_frame(
    detections: list[FaceDetection],
) -> dict[int, list[FaceDetection]]:
    grouped: dict[int, list[FaceDetection]] = {}
    for det in detections:
        grouped.setdefault(det.frame_index, []).append(det)
    return grouped


def _is_valid_detection(detection: FaceDetection, frame_w: int, frame_h: int) -> bool:
    bbox = detection.bbox
    if bbox.width <= 0 or bbox.height <= 0:
        return False
    if bbox.x < 0 or bbox.y < 0:
        return False
    if bbox.x + bbox.width > frame_w or bbox.y + bbox.height > frame_h:
        return False
    if not math.isfinite(detection.confidence) or detection.confidence < 0:
        return False
    return True


def _bbox_center(bbox: BoundingBox) -> tuple[float, float]:
    return (bbox.x + (bbox.width / 2.0), bbox.y + (bbox.height / 2.0))


def _failure_track_report(
    *,
    input_path: str,
    reason: str,
    message: str,
    frames_sampled: int = 0,
) -> TrackReport:
    return TrackReport(
        ok=False,
        usable=False,
        input_path=input_path,
        frames_sampled=frames_sampled,
        reason=reason,
        message=message,
    )
