"""Structured types for reframing face detection."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class BoundingBox:
    """Face bounding box in source-frame pixel coordinates."""

    x: int
    y: int
    width: int
    height: int

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class FrameSample:
    """One frame extracted from a clip for detection."""

    timestamp_sec: float
    frame_path: str
    frame_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_sec": round(self.timestamp_sec, 3),
            "frame_path": self.frame_path,
            "frame_index": self.frame_index,
        }


@dataclass(frozen=True)
class FaceDetection:
    """One detected face at a sampled timestamp."""

    timestamp_sec: float
    bbox: BoundingBox
    confidence: float
    frame_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_sec": round(self.timestamp_sec, 3),
            "bbox": self.bbox.to_dict(),
            "confidence": round(self.confidence, 4),
            "frame_index": self.frame_index,
        }


@dataclass
class DetectionReport:
    """Aggregate detection result for one clip sample pass."""

    ok: bool
    input_path: str
    detection_fps: float
    detector_backend: str
    frames_sampled: int = 0
    frames_with_faces: int = 0
    faces_detected_pct: float = 0.0
    frame_width: int | None = None
    frame_height: int | None = None
    detections: list[FaceDetection] = field(default_factory=list)
    reason: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["detections"] = [d.to_dict() for d in self.detections]
        payload["faces_detected_pct"] = round(self.faces_detected_pct, 2)
        return payload


@dataclass(frozen=True)
class TrackedFaceSample:
    """One primary-face sample on the speaker track timeline."""

    timestamp_sec: float
    frame_index: int
    bbox: BoundingBox | None
    confidence: float | None
    missing: bool
    held: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp_sec": round(self.timestamp_sec, 3),
            "frame_index": self.frame_index,
            "missing": self.missing,
            "held": self.held,
        }
        if self.bbox is not None:
            payload["bbox"] = self.bbox.to_dict()
        if self.confidence is not None:
            payload["confidence"] = round(self.confidence, 4)
        return payload


@dataclass
class TrackReport:
    """Primary single-speaker face track derived from a detection report."""

    ok: bool
    usable: bool
    input_path: str
    source: str = "detection_report"
    frames_sampled: int = 0
    track_samples: int = 0
    track_coverage_pct: float = 0.0
    max_gap_sec: float = 0.0
    primary_selection: str = "largest_confident_centre_weighted"
    track: list[TrackedFaceSample] = field(default_factory=list)
    reason: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["track"] = [sample.to_dict() for sample in self.track]
        payload["track_coverage_pct"] = round(self.track_coverage_pct, 2)
        payload["max_gap_sec"] = round(self.max_gap_sec, 3)
        return payload


@dataclass(frozen=True)
class CropRect:
    """A 9:16 crop window in source-frame pixel coordinates."""

    x: int
    y: int
    width: int
    height: int

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class CropPathSample:
    """One timestamped crop rectangle on the planned face-track path."""

    timestamp_sec: float
    frame_index: int
    crop: CropRect
    held: bool
    source_bbox: BoundingBox | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp_sec": round(self.timestamp_sec, 3),
            "frame_index": self.frame_index,
            "x": self.crop.x,
            "y": self.crop.y,
            "width": self.crop.width,
            "height": self.crop.height,
            "held": self.held,
        }
        if self.source_bbox is not None:
            payload["source_bbox"] = self.source_bbox.to_dict()
        return payload


@dataclass
class CropPathReport:
    """Timestamped 9:16 crop path derived from a usable face track."""

    ok: bool
    usable: bool
    input_path: str
    source: str = "track_report"
    source_width: int = 0
    source_height: int = 0
    target_width: int = 1080
    target_height: int = 1920
    target_aspect: float = 9 / 16
    crop_width: int = 0
    crop_height: int = 0
    head_vertical_ratio: float = 0.35
    samples: list[CropPathSample] = field(default_factory=list)
    smoothed: bool = False
    smoothing_method: str | None = None
    raw_samples: list[CropPathSample] = field(default_factory=list)
    smoothing: dict[str, Any] | None = None
    movement_stats: dict[str, Any] | None = None
    reason: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["samples"] = [sample.to_dict() for sample in self.samples]
        payload["raw_samples"] = [sample.to_dict() for sample in self.raw_samples]
        payload["target_aspect"] = round(self.target_aspect, 6)
        payload["head_vertical_ratio"] = round(self.head_vertical_ratio, 4)
        if not self.smoothed:
            payload.pop("smoothing_method", None)
            if not self.raw_samples:
                payload.pop("raw_samples", None)
            if not self.smoothing:
                payload.pop("smoothing", None)
            if not self.movement_stats:
                payload.pop("movement_stats", None)
        return payload


@dataclass(frozen=True)
class RenderSegment:
    """One time interval and crop rectangle for segmented face-track rendering."""

    start_sec: float
    end_sec: float
    crop: CropRect
    sample_index: int
    held: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_sec": round(self.start_sec, 3),
            "end_sec": round(self.end_sec, 3),
            "x": self.crop.x,
            "y": self.crop.y,
            "width": self.crop.width,
            "height": self.crop.height,
            "sample_index": self.sample_index,
            "held": self.held,
        }


@dataclass(frozen=True)
class RenderSegmentPlanStats:
    """Planning statistics for segmented face-track crop rendering."""

    segments_planned: int = 0
    segments_rendered: int = 0
    segments_merged: int = 0
    unique_crop_rects_before_merge: int = 0
    unique_crop_rects_after_merge: int = 0
    segment_crop_change_threshold_px: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FaceTrackRenderResult:
    """Result of a segmented FFmpeg face-track crop render."""

    ok: bool
    output_path: str | None = None
    crop_renderer: str = "segmented_ffmpeg"
    segments_planned: int = 0
    segments_rendered: int = 0
    segments_merged: int = 0
    unique_crop_rects_before_merge: int = 0
    unique_crop_rects_after_merge: int = 0
    segment_crop_change_threshold_px: int = 0
    target_width: int = 0
    target_height: int = 0
    ffmpeg_command_summary: str | None = None
    reason: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FaceTrackEligibilityReport:
    """Face-track eligibility decision for one clip."""

    ok: bool
    eligible: bool
    reason: str
    face_coverage_pct: float = 0.0
    max_no_face_gap_sec: float = 0.0
    longest_face_run_sec: float = 0.0
    leading_no_face_gap_sec: float = 0.0
    trailing_no_face_gap_sec: float = 0.0
    longest_face_run_pct: float = 0.0
    crop_x_range_px: float = 0.0
    crop_x_range_pct_of_source_width: float = 0.0
    crop_center_x_range_px: float = 0.0
    max_adjacent_crop_x_jump_px: float = 0.0
    max_adjacent_crop_x_jump_pct_of_crop_width: float = 0.0
    layout_risk: bool = False
    track_usable: bool = False
    multi_face_risk: bool = False
    primary_track_dominance_pct: float = 100.0
    frames_sampled: int = 0
    frames_with_faces: int = 0
    track_samples: int = 0
    message: str | None = None
    thresholds: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_sidecar_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "eligible": self.eligible,
            "reason": self.reason,
            "message": self.message,
            "thresholds": self.thresholds,
            "metrics": self.metrics,
            "face_coverage_pct": round(self.face_coverage_pct, 2),
            "max_no_face_gap_sec": round(self.max_no_face_gap_sec, 3),
            "longest_face_run_sec": round(self.longest_face_run_sec, 3),
            "leading_no_face_gap_sec": round(self.leading_no_face_gap_sec, 3),
            "trailing_no_face_gap_sec": round(self.trailing_no_face_gap_sec, 3),
            "longest_face_run_pct": round(self.longest_face_run_pct, 2),
            "crop_x_range_px": round(self.crop_x_range_px, 2),
            "crop_x_range_pct_of_source_width": round(self.crop_x_range_pct_of_source_width, 2),
            "max_adjacent_crop_x_jump_px": round(self.max_adjacent_crop_x_jump_px, 2),
            "max_adjacent_crop_x_jump_pct_of_crop_width": round(
                self.max_adjacent_crop_x_jump_pct_of_crop_width,
                2,
            ),
            "layout_risk": self.layout_risk,
            "track_usable": self.track_usable,
            "multi_face_risk": self.multi_face_risk,
            "primary_track_dominance_pct": round(self.primary_track_dominance_pct, 2),
        }
