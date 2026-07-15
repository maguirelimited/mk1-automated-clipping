"""Virtual-camera smoothing for face-tracked crop paths."""

from __future__ import annotations

import copy
import math
from typing import Any

from reframing.crop_path_planner import (
    ASPECT_TOLERANCE,
    build_face_crop_path_for_clip,
    write_crop_path_report,
)
from reframing.types import CropPathReport, CropPathSample, CropRect, DetectionReport, TrackReport

# ---------------------------------------------------------------------------
# Defaults (internal — not exposed in Ops UI yet)
# ---------------------------------------------------------------------------

DEFAULT_SMOOTHING_ENABLED = True
DEFAULT_DEADZONE_PX = 8
DEFAULT_EMA_ALPHA = 0.25
DEFAULT_MAX_VELOCITY_PX_PER_SEC = 900
DEFAULT_ROUND_EVEN_POSITIONS = True

SMOOTHING_METHOD = "deadzone_ema_velocity_cap"

REASON_CROP_PATH_NOT_USABLE = "crop_path_not_usable"
REASON_SMOOTHING_DISABLED = "smoothing_disabled"
REASON_INVALID_SMOOTHED_CROP = "invalid_smoothed_crop"
REASON_NO_CROP_SAMPLES = "no_crop_samples"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def merge_smoother_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge caller overrides with crop smoother defaults."""
    merged = {
        "smoothing_enabled": DEFAULT_SMOOTHING_ENABLED,
        "deadzone_px": DEFAULT_DEADZONE_PX,
        "ema_alpha": DEFAULT_EMA_ALPHA,
        "max_velocity_px_per_sec": DEFAULT_MAX_VELOCITY_PX_PER_SEC,
        "round_even_positions": DEFAULT_ROUND_EVEN_POSITIONS,
    }
    if config:
        for key in merged:
            if key in config:
                merged[key] = config[key]
    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def smooth_crop_path(
    crop_report: CropPathReport,
    config: dict[str, Any] | None = None,
) -> CropPathReport:
    """Smooth a raw crop path into a stable virtual-camera path."""
    merged = merge_smoother_config(config)

    if not crop_report.ok or not crop_report.usable:
        return _failure_smoothed_report(
            crop_report=crop_report,
            reason=REASON_CROP_PATH_NOT_USABLE,
            message="Cannot smooth an unusable crop path.",
        )

    if not crop_report.samples:
        return _failure_smoothed_report(
            crop_report=crop_report,
            reason=REASON_NO_CROP_SAMPLES,
            message="Cannot smooth a crop path with no samples.",
        )

    if not merged["smoothing_enabled"]:
        return CropPathReport(
            ok=True,
            usable=True,
            input_path=crop_report.input_path,
            source=crop_report.source,
            source_width=crop_report.source_width,
            source_height=crop_report.source_height,
            target_width=crop_report.target_width,
            target_height=crop_report.target_height,
            target_aspect=crop_report.target_aspect,
            crop_width=crop_report.crop_width,
            crop_height=crop_report.crop_height,
            head_vertical_ratio=crop_report.head_vertical_ratio,
            samples=copy.deepcopy(crop_report.samples),
            smoothed=False,
            reason=REASON_SMOOTHING_DISABLED,
            message="Smoothing is disabled.",
        )

    deadzone_px = float(merged["deadzone_px"])
    alpha = float(merged["ema_alpha"])
    max_velocity = float(merged["max_velocity_px_per_sec"])
    round_even = bool(merged["round_even_positions"])

    crop_width = crop_report.crop_width
    crop_height = crop_report.crop_height
    source_width = crop_report.source_width
    source_height = crop_report.source_height
    target_aspect = crop_report.target_aspect

    raw_samples = copy.deepcopy(crop_report.samples)
    smoothed_samples: list[CropPathSample] = []

    prev_cx: float | None = None
    prev_cy: float | None = None
    prev_timestamp: float | None = None

    raw_total_movement = 0.0
    smoothed_total_movement = 0.0
    samples_adjusted = 0

    prev_raw_center: tuple[float, float] | None = None
    prev_smooth_center: tuple[float, float] | None = None

    for raw_sample in raw_samples:
        raw_rect = raw_sample.crop
        raw_cx, raw_cy = _crop_center(raw_rect)

        if prev_raw_center is not None:
            raw_total_movement += _distance(raw_cx, raw_cy, *prev_raw_center)
        prev_raw_center = (raw_cx, raw_cy)

        if prev_cx is None or prev_cy is None:
            smooth_cx, smooth_cy = raw_cx, raw_cy
            output_held = raw_sample.held
        elif raw_sample.held:
            smooth_cx, smooth_cy = prev_cx, prev_cy
            output_held = True
        else:
            target_cx, target_cy = raw_cx, raw_cy
            if _distance(target_cx, target_cy, prev_cx, prev_cy) < deadzone_px:
                smooth_cx, smooth_cy = prev_cx, prev_cy
            else:
                smooth_cx = prev_cx + alpha * (target_cx - prev_cx)
                smooth_cy = prev_cy + alpha * (target_cy - prev_cy)

            if prev_timestamp is None:
                dt = 0.0
            else:
                dt = raw_sample.timestamp_sec - prev_timestamp
            if dt <= 0:
                dt = 1e-3
            max_move = max_velocity * dt
            dx = smooth_cx - prev_cx
            dy = smooth_cy - prev_cy
            move_dist = math.hypot(dx, dy)
            if move_dist > max_move and move_dist > 0:
                scale = max_move / move_dist
                smooth_cx = prev_cx + dx * scale
                smooth_cy = prev_cy + dy * scale

            output_held = raw_sample.held

        smooth_cx, smooth_cy = _clamp_crop_center(
            smooth_cx,
            smooth_cy,
            crop_width=crop_width,
            crop_height=crop_height,
            source_width=source_width,
            source_height=source_height,
        )

        smooth_rect = _center_to_crop(
            smooth_cx,
            smooth_cy,
            crop_width=crop_width,
            crop_height=crop_height,
            round_even=round_even,
        )

        if not _crop_rect_is_valid(
            smooth_rect,
            source_width=source_width,
            source_height=source_height,
            target_aspect=target_aspect,
        ):
            return _failure_smoothed_report(
                crop_report=crop_report,
                reason=REASON_INVALID_SMOOTHED_CROP,
                message=(
                    f"Invalid smoothed crop rectangle generated for frame "
                    f"{raw_sample.frame_index}."
                ),
                raw_samples=raw_samples,
            )

        if prev_smooth_center is not None:
            scx, scy = _crop_center(smooth_rect)
            smoothed_total_movement += _distance(scx, scy, *prev_smooth_center)
        prev_smooth_center = _crop_center(smooth_rect)

        if smooth_rect.x != raw_rect.x or smooth_rect.y != raw_rect.y:
            samples_adjusted += 1

        smoothed_samples.append(
            CropPathSample(
                timestamp_sec=raw_sample.timestamp_sec,
                frame_index=raw_sample.frame_index,
                crop=smooth_rect,
                held=output_held,
                source_bbox=raw_sample.source_bbox,
            )
        )

        prev_cx, prev_cy = _crop_center(smooth_rect)
        prev_timestamp = raw_sample.timestamp_sec

    smoothing_config = {
        "deadzone_px": deadzone_px,
        "ema_alpha": alpha,
        "max_velocity_px_per_sec": max_velocity,
    }
    movement_stats = {
        "raw_total_movement_px": round(raw_total_movement, 2),
        "smoothed_total_movement_px": round(smoothed_total_movement, 2),
        "samples_adjusted": samples_adjusted,
    }

    return CropPathReport(
        ok=True,
        usable=True,
        input_path=crop_report.input_path,
        source="crop_path_report",
        source_width=source_width,
        source_height=source_height,
        target_width=crop_report.target_width,
        target_height=crop_report.target_height,
        target_aspect=target_aspect,
        crop_width=crop_width,
        crop_height=crop_height,
        head_vertical_ratio=crop_report.head_vertical_ratio,
        samples=smoothed_samples,
        smoothed=True,
        smoothing_method=SMOOTHING_METHOD,
        raw_samples=raw_samples,
        smoothing=smoothing_config,
        movement_stats=movement_stats,
    )


def build_smoothed_face_crop_path_for_clip(
    input_path: str,
    *,
    source_width: int,
    source_height: int,
    tmp_dir: str,
    config: dict[str, Any] | None = None,
    detection_report_path: str | None = None,
    track_report_path: str | None = None,
    crop_path_report_path: str | None = None,
    smoothed_crop_path_report_path: str | None = None,
) -> tuple[DetectionReport, TrackReport, CropPathReport, CropPathReport]:
    """Run detection, tracking, crop planning, and smoothing for one clip."""
    detection_report, track_report, crop_report = build_face_crop_path_for_clip(
        input_path,
        source_width=source_width,
        source_height=source_height,
        tmp_dir=tmp_dir,
        config=config,
        detection_report_path=detection_report_path,
        track_report_path=track_report_path,
        crop_path_report_path=crop_path_report_path,
    )
    smoothed_report = smooth_crop_path(crop_report, config=config)
    if smoothed_crop_path_report_path:
        write_smoothed_crop_path_report(smoothed_crop_path_report_path, smoothed_report)
    return detection_report, track_report, crop_report, smoothed_report


def write_smoothed_crop_path_report(path: str, report: CropPathReport) -> None:
    """Write a smoothed crop-path sidecar JSON file for debugging."""
    write_crop_path_report(path, report)


def smoothed_crop_path_metadata(
    *,
    smoothed_report: CropPathReport | None,
) -> dict[str, Any]:
    """Build safe metadata fields for platform_safe_format_v1."""
    metadata: dict[str, Any] = {
        "smoothing_attempted": smoothed_report is not None,
    }
    if smoothed_report is None:
        return metadata

    metadata["smoothed_crop_path_ok"] = smoothed_report.ok
    metadata["smoothed_crop_path_usable"] = smoothed_report.usable
    metadata["smoothed_crop_path_samples"] = len(smoothed_report.samples)
    if smoothed_report.smoothing_method:
        metadata["smoothing_method"] = smoothed_report.smoothing_method
    if smoothed_report.movement_stats:
        metadata["raw_total_movement_px"] = smoothed_report.movement_stats.get(
            "raw_total_movement_px"
        )
        metadata["smoothed_total_movement_px"] = smoothed_report.movement_stats.get(
            "smoothed_total_movement_px"
        )
        metadata["smoothing_samples_adjusted"] = smoothed_report.movement_stats.get(
            "samples_adjusted"
        )
    if smoothed_report.reason and not smoothed_report.usable:
        metadata["smoothed_crop_path_reason"] = smoothed_report.reason
    return metadata


# ---------------------------------------------------------------------------
# Smoothing geometry
# ---------------------------------------------------------------------------


def _crop_center(crop: CropRect) -> tuple[float, float]:
    return (crop.x + (crop.width / 2.0), crop.y + (crop.height / 2.0))


def _center_to_crop(
    cx: float,
    cy: float,
    *,
    crop_width: int,
    crop_height: int,
    round_even: bool,
) -> CropRect:
    x = int(round(cx - (crop_width / 2.0)))
    y = int(round(cy - (crop_height / 2.0)))
    if round_even:
        x -= x % 2
        y -= y % 2
    return CropRect(x=x, y=y, width=crop_width, height=crop_height)


def _clamp_crop_center(
    cx: float,
    cy: float,
    *,
    crop_width: int,
    crop_height: int,
    source_width: int,
    source_height: int,
) -> tuple[float, float]:
    half_w = crop_width / 2.0
    half_h = crop_height / 2.0
    cx = max(half_w, min(cx, source_width - half_w))
    cy = max(half_h, min(cy, source_height - half_h))
    return cx, cy


def _distance(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.hypot(x1 - x2, y1 - y2)


def _crop_rect_is_valid(
    crop: CropRect,
    *,
    source_width: int,
    source_height: int,
    target_aspect: float,
) -> bool:
    if crop.width <= 0 or crop.height <= 0:
        return False
    if crop.x < 0 or crop.y < 0:
        return False
    if crop.x + crop.width > source_width:
        return False
    if crop.y + crop.height > source_height:
        return False

    actual_aspect = crop.width / crop.height
    return abs(actual_aspect - target_aspect) <= ASPECT_TOLERANCE


def _failure_smoothed_report(
    *,
    crop_report: CropPathReport,
    reason: str,
    message: str,
    raw_samples: list[CropPathSample] | None = None,
) -> CropPathReport:
    return CropPathReport(
        ok=False,
        usable=False,
        input_path=crop_report.input_path,
        source="crop_path_report",
        source_width=crop_report.source_width,
        source_height=crop_report.source_height,
        target_width=crop_report.target_width,
        target_height=crop_report.target_height,
        target_aspect=crop_report.target_aspect,
        crop_width=crop_report.crop_width,
        crop_height=crop_report.crop_height,
        head_vertical_ratio=crop_report.head_vertical_ratio,
        samples=[],
        smoothed=False,
        smoothing_method=SMOOTHING_METHOD,
        raw_samples=raw_samples or [],
        reason=reason,
        message=message,
    )
