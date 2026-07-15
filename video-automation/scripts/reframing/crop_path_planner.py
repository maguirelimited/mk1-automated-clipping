"""9:16 crop path planning from a primary face track."""

from __future__ import annotations

import json
import math
import os
from typing import Any

from reframing.detector import detect_faces_for_clip
from reframing.tracker import build_face_track_for_clip, build_primary_face_track
from reframing.types import (
    BoundingBox,
    CropPathReport,
    CropPathSample,
    CropRect,
    DetectionReport,
    TrackReport,
    TrackedFaceSample,
)

# ---------------------------------------------------------------------------
# Defaults (internal — not exposed in Ops UI yet)
# ---------------------------------------------------------------------------

DEFAULT_TARGET_WIDTH = 1080
DEFAULT_TARGET_HEIGHT = 1920
DEFAULT_TARGET_ASPECT_WIDTH = 9
DEFAULT_TARGET_ASPECT_HEIGHT = 16
DEFAULT_HEAD_VERTICAL_RATIO = 0.35
DEFAULT_MIN_CROP_WIDTH = 240
DEFAULT_MIN_CROP_HEIGHT = 426
DEFAULT_ROUND_EVEN_DIMENSIONS = True

ASPECT_TOLERANCE = 0.02

REASON_TRACK_NOT_USABLE = "track_not_usable"
REASON_INVALID_SOURCE_DIMENSIONS = "invalid_source_dimensions"
REASON_INVALID_CROP_DIMENSIONS = "invalid_crop_dimensions"
REASON_NO_TRACK_SAMPLES = "no_track_samples"
REASON_MISSING_FACE_BBOX = "missing_face_bbox"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def merge_crop_planner_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge caller overrides with crop planner defaults."""
    merged = {
        "target_width": DEFAULT_TARGET_WIDTH,
        "target_height": DEFAULT_TARGET_HEIGHT,
        "target_aspect_width": DEFAULT_TARGET_ASPECT_WIDTH,
        "target_aspect_height": DEFAULT_TARGET_ASPECT_HEIGHT,
        "head_vertical_ratio": DEFAULT_HEAD_VERTICAL_RATIO,
        "min_crop_width": DEFAULT_MIN_CROP_WIDTH,
        "min_crop_height": DEFAULT_MIN_CROP_HEIGHT,
        "round_even_dimensions": DEFAULT_ROUND_EVEN_DIMENSIONS,
    }
    if config:
        if "target_width" in config:
            merged["target_width"] = config["target_width"]
        if "target_height" in config:
            merged["target_height"] = config["target_height"]
        for key in (
            "target_aspect_width",
            "target_aspect_height",
            "head_vertical_ratio",
            "min_crop_width",
            "min_crop_height",
            "round_even_dimensions",
        ):
            if key in config:
                merged[key] = config[key]
    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_crop_path_from_track(
    track_report: TrackReport,
    *,
    source_width: int,
    source_height: int,
    config: dict[str, Any] | None = None,
) -> CropPathReport:
    """Build a timestamped 9:16 crop path from a primary face track."""
    merged = merge_crop_planner_config(config)
    target_width = int(merged["target_width"])
    target_height = int(merged["target_height"])
    aspect_w = int(merged["target_aspect_width"])
    aspect_h = int(merged["target_aspect_height"])
    head_vertical_ratio = float(merged["head_vertical_ratio"])
    min_crop_width = int(merged["min_crop_width"])
    min_crop_height = int(merged["min_crop_height"])
    round_even = bool(merged["round_even_dimensions"])
    target_aspect = aspect_w / aspect_h

    if source_width <= 0 or source_height <= 0:
        return _failure_crop_report(
            input_path=track_report.input_path,
            source_width=source_width,
            source_height=source_height,
            target_width=target_width,
            target_height=target_height,
            target_aspect=target_aspect,
            head_vertical_ratio=head_vertical_ratio,
            reason=REASON_INVALID_SOURCE_DIMENSIONS,
            message=f"Invalid source dimensions: {source_width}x{source_height}",
        )

    if not track_report.ok or not track_report.usable:
        return _failure_crop_report(
            input_path=track_report.input_path,
            source_width=source_width,
            source_height=source_height,
            target_width=target_width,
            target_height=target_height,
            target_aspect=target_aspect,
            head_vertical_ratio=head_vertical_ratio,
            reason=REASON_TRACK_NOT_USABLE,
            message="Cannot create crop path because face track is not usable.",
        )

    if not track_report.track:
        return _failure_crop_report(
            input_path=track_report.input_path,
            source_width=source_width,
            source_height=source_height,
            target_width=target_width,
            target_height=target_height,
            target_aspect=target_aspect,
            head_vertical_ratio=head_vertical_ratio,
            reason=REASON_NO_TRACK_SAMPLES,
            message="Track report contains no samples.",
        )

    crop_width, crop_height, crop_error = compute_max_crop_dimensions(
        source_width,
        source_height,
        aspect_width=aspect_w,
        aspect_height=aspect_h,
        round_even=round_even,
    )
    if crop_error:
        return _failure_crop_report(
            input_path=track_report.input_path,
            source_width=source_width,
            source_height=source_height,
            target_width=target_width,
            target_height=target_height,
            target_aspect=target_aspect,
            head_vertical_ratio=head_vertical_ratio,
            reason=REASON_INVALID_CROP_DIMENSIONS,
            message=crop_error,
        )

    if crop_width < min_crop_width or crop_height < min_crop_height:
        return _failure_crop_report(
            input_path=track_report.input_path,
            source_width=source_width,
            source_height=source_height,
            target_width=target_width,
            target_height=target_height,
            target_aspect=target_aspect,
            head_vertical_ratio=head_vertical_ratio,
            crop_width=crop_width,
            crop_height=crop_height,
            reason=REASON_INVALID_CROP_DIMENSIONS,
            message=(
                f"Computed crop {crop_width}x{crop_height} is below minimum "
                f"{min_crop_width}x{min_crop_height}."
            ),
        )

    samples: list[CropPathSample] = []
    for track_sample in track_report.track:
        if track_sample.bbox is None:
            return _failure_crop_report(
                input_path=track_report.input_path,
                source_width=source_width,
                source_height=source_height,
                target_width=target_width,
                target_height=target_height,
                target_aspect=target_aspect,
                head_vertical_ratio=head_vertical_ratio,
                crop_width=crop_width,
                crop_height=crop_height,
                reason=REASON_MISSING_FACE_BBOX,
                message=(
                    f"Track sample at frame {track_sample.frame_index} "
                    "is missing a bounding box."
                ),
            )

        crop_rect = position_crop_for_face(
            track_sample.bbox,
            crop_width=crop_width,
            crop_height=crop_height,
            source_width=source_width,
            source_height=source_height,
            head_vertical_ratio=head_vertical_ratio,
        )
        if not _crop_rect_is_valid(
            crop_rect,
            source_width=source_width,
            source_height=source_height,
            target_aspect=target_aspect,
        ):
            return _failure_crop_report(
                input_path=track_report.input_path,
                source_width=source_width,
                source_height=source_height,
                target_width=target_width,
                target_height=target_height,
                target_aspect=target_aspect,
                head_vertical_ratio=head_vertical_ratio,
                crop_width=crop_width,
                crop_height=crop_height,
                reason=REASON_INVALID_CROP_DIMENSIONS,
                message=(
                    f"Invalid crop rectangle generated for frame "
                    f"{track_sample.frame_index}."
                ),
            )

        samples.append(
            CropPathSample(
                timestamp_sec=track_sample.timestamp_sec,
                frame_index=track_sample.frame_index,
                crop=crop_rect,
                held=track_sample.held or track_sample.missing,
                source_bbox=track_sample.bbox,
            )
        )

    return CropPathReport(
        ok=True,
        usable=True,
        input_path=track_report.input_path,
        source_width=source_width,
        source_height=source_height,
        target_width=target_width,
        target_height=target_height,
        target_aspect=target_aspect,
        crop_width=crop_width,
        crop_height=crop_height,
        head_vertical_ratio=head_vertical_ratio,
        samples=samples,
    )


def build_face_crop_path_for_clip(
    input_path: str,
    *,
    source_width: int,
    source_height: int,
    tmp_dir: str,
    config: dict[str, Any] | None = None,
    detection_report_path: str | None = None,
    track_report_path: str | None = None,
    crop_path_report_path: str | None = None,
) -> tuple[DetectionReport, TrackReport, CropPathReport]:
    """Run detection, tracking, and crop-path planning for one clip."""
    detection_report, track_report = build_face_track_for_clip(
        input_path,
        tmp_dir=tmp_dir,
        config=config,
        detection_report_path=detection_report_path,
        track_report_path=track_report_path,
    )
    crop_report = build_crop_path_from_track(
        track_report,
        source_width=source_width,
        source_height=source_height,
        config=config,
    )
    if crop_path_report_path:
        write_crop_path_report(crop_path_report_path, crop_report)
    return detection_report, track_report, crop_report


def write_crop_path_report(path: str, report: CropPathReport) -> None:
    """Write a crop-path sidecar JSON file for debugging."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2, sort_keys=True)
        fh.write("\n")


def crop_path_metadata(*, crop_report: CropPathReport | None) -> dict[str, Any]:
    """Build safe metadata fields for platform_safe_format_v1."""
    metadata: dict[str, Any] = {
        "crop_path_attempted": crop_report is not None,
    }
    if crop_report is None:
        return metadata

    metadata["crop_path_ok"] = crop_report.ok
    metadata["crop_path_usable"] = crop_report.usable
    metadata["crop_path_samples"] = len(crop_report.samples)
    metadata["crop_width"] = crop_report.crop_width
    metadata["crop_height"] = crop_report.crop_height
    metadata["head_vertical_ratio"] = round(crop_report.head_vertical_ratio, 4)
    if crop_report.reason:
        metadata["crop_path_reason"] = crop_report.reason
    return metadata


# ---------------------------------------------------------------------------
# Crop geometry
# ---------------------------------------------------------------------------


def compute_max_crop_dimensions(
    source_width: int,
    source_height: int,
    *,
    aspect_width: int = DEFAULT_TARGET_ASPECT_WIDTH,
    aspect_height: int = DEFAULT_TARGET_ASPECT_HEIGHT,
    round_even: bool = DEFAULT_ROUND_EVEN_DIMENSIONS,
) -> tuple[int, int, str | None]:
    """Return the largest 9:16 crop that fits inside the source frame."""
    if source_width <= 0 or source_height <= 0:
        return 0, 0, f"invalid source dimensions: {source_width}x{source_height}"

    target_aspect = aspect_width / aspect_height
    source_aspect = source_width / source_height

    if source_aspect > target_aspect:
        crop_height = source_height
        crop_width = crop_height * target_aspect
    else:
        crop_width = source_width
        crop_height = crop_width / target_aspect

    crop_width = int(round(crop_width))
    crop_height = int(round(crop_height))

    if round_even:
        crop_width -= crop_width % 2
        crop_height -= crop_height % 2

    crop_width = max(2, min(crop_width, source_width))
    crop_height = max(2, min(crop_height, source_height))

    if crop_width <= 0 or crop_height <= 0:
        return 0, 0, "computed crop dimensions are non-positive"

    return crop_width, crop_height, None


def position_crop_for_face(
    bbox: BoundingBox,
    *,
    crop_width: int,
    crop_height: int,
    source_width: int,
    source_height: int,
    head_vertical_ratio: float,
) -> CropRect:
    """Place a crop window from a face bbox with upper-third head positioning."""
    face_cx = bbox.x + (bbox.width / 2.0)
    face_cy = bbox.y + (bbox.height / 2.0)

    crop_x = face_cx - (crop_width / 2.0)
    crop_y = face_cy - (crop_height * head_vertical_ratio)

    crop_x = _clamp(crop_x, 0.0, float(source_width - crop_width))
    crop_y = _clamp(crop_y, 0.0, float(source_height - crop_height))

    return CropRect(
        x=int(round(crop_x)),
        y=int(round(crop_y)),
        width=crop_width,
        height=crop_height,
    )


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


def _bbox_center(bbox: BoundingBox) -> tuple[float, float]:
    return (bbox.x + (bbox.width / 2.0), bbox.y + (bbox.height / 2.0))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def _failure_crop_report(
    *,
    input_path: str,
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
    target_aspect: float,
    head_vertical_ratio: float,
    reason: str,
    message: str,
    crop_width: int = 0,
    crop_height: int = 0,
) -> CropPathReport:
    return CropPathReport(
        ok=False,
        usable=False,
        input_path=input_path,
        source_width=source_width,
        source_height=source_height,
        target_width=target_width,
        target_height=target_height,
        target_aspect=target_aspect,
        crop_width=crop_width,
        crop_height=crop_height,
        head_vertical_ratio=head_vertical_ratio,
        reason=reason,
        message=message,
    )
