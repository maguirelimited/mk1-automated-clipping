"""Focused tests for reframing.tracker — primary face track building."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from reframing.tracker import (
    REASON_EXCESSIVE_GAP,
    REASON_INSUFFICIENT_TRACK_COVERAGE,
    REASON_NO_DETECTIONS,
    build_face_track_for_clip,
    build_primary_face_track,
    write_track_report,
)
from reframing.types import BoundingBox, DetectionReport, FaceDetection, TrackReport


def _bbox(x: int, y: int, w: int, h: int) -> BoundingBox:
    return BoundingBox(x=x, y=y, width=w, height=h)


def _det(
    frame_index: int,
    *,
    x: int,
    y: int,
    w: int,
    h: int,
    confidence: float = 0.9,
    fps: float = 2.0,
) -> FaceDetection:
    return FaceDetection(
        timestamp_sec=frame_index / fps,
        frame_index=frame_index,
        bbox=_bbox(x, y, w, h),
        confidence=confidence,
    )


def _report(
    detections: list[FaceDetection],
    *,
    frames_sampled: int | None = None,
    fps: float = 2.0,
) -> DetectionReport:
    sampled = frames_sampled if frames_sampled is not None else (max((d.frame_index for d in detections), default=-1) + 1)
    frames_with_faces = len({d.frame_index for d in detections})
    pct = (frames_with_faces / sampled * 100.0) if sampled else 0.0
    return DetectionReport(
        ok=True,
        input_path="/in.mp4",
        detection_fps=fps,
        detector_backend="mediapipe",
        frames_sampled=sampled,
        frames_with_faces=frames_with_faces,
        faces_detected_pct=pct,
        frame_width=1000,
        frame_height=800,
        detections=detections,
    )


def test_no_detections_not_usable():
    report = build_primary_face_track(
        DetectionReport(
            ok=True,
            input_path="/in.mp4",
            detection_fps=2.0,
            detector_backend="mediapipe",
            frames_sampled=5,
            detections=[],
        )
    )
    assert report.ok is False
    assert report.usable is False
    assert report.reason == REASON_NO_DETECTIONS


def test_single_face_across_frames_is_usable():
    detections = [
        _det(0, x=400, y=100, w=180, h=180),
        _det(1, x=405, y=102, w=180, h=180),
        _det(2, x=410, y=104, w=180, h=180),
    ]
    report = build_primary_face_track(_report(detections, frames_sampled=3))
    assert report.ok is True
    assert report.usable is True
    assert report.track_samples == 3
    assert report.track_coverage_pct == 100.0
    assert all(not sample.missing for sample in report.track)


def test_multiple_faces_choose_dominant_face_deterministically():
    detections = [
        _det(0, x=100, y=100, w=80, h=80, confidence=0.7),
        _det(0, x=400, y=100, w=220, h=220, confidence=0.95),
        _det(1, x=405, y=102, w=220, h=220, confidence=0.94),
        _det(2, x=410, y=104, w=220, h=220, confidence=0.93),
    ]
    report = build_primary_face_track(_report(detections, frames_sampled=3))
    assert report.usable is True
    assert report.track[0].bbox is not None
    assert report.track[0].bbox.width == 220


def test_short_gap_bridged_with_held_sample():
    detections = [
        _det(0, x=400, y=100, w=180, h=180),
        _det(1, x=402, y=101, w=180, h=180),
        _det(3, x=410, y=104, w=180, h=180),
        _det(4, x=412, y=105, w=180, h=180),
    ]
    report = build_primary_face_track(_report(detections, frames_sampled=5))
    assert report.track[2].missing is True
    assert report.track[2].held is True
    assert report.max_gap_sec == 0.5
    assert report.usable is True


def test_long_gap_makes_track_not_usable():
    detections = [
        _det(0, x=400, y=100, w=180, h=180),
        _det(1, x=402, y=101, w=180, h=180),
        _det(2, x=404, y=102, w=180, h=180),
        _det(7, x=410, y=104, w=180, h=180),
    ]
    report = build_primary_face_track(_report(detections, frames_sampled=8))
    assert report.ok is True
    assert report.usable is False
    assert report.reason == REASON_EXCESSIVE_GAP
    assert report.max_gap_sec == 2.0


def test_low_coverage_not_usable():
    detections = [
        _det(0, x=400, y=100, w=180, h=180),
        _det(1, x=402, y=101, w=180, h=180),
        _det(2, x=404, y=102, w=180, h=180),
    ]
    report = build_primary_face_track(_report(detections, frames_sampled=20))
    assert report.ok is True
    assert report.usable is False
    assert report.reason == REASON_INSUFFICIENT_TRACK_COVERAGE


def test_invalid_bbox_rejected():
    detections = [_det(0, x=900, y=100, w=500, h=500)]
    report = build_primary_face_track(_report(detections, frames_sampled=1))
    assert report.ok is False
    assert report.usable is False


def test_write_track_report_json(tmp_path):
    report = TrackReport(
        ok=True,
        usable=True,
        input_path="/in.mp4",
        frames_sampled=2,
        track_samples=2,
        track_coverage_pct=100.0,
        max_gap_sec=0.0,
        track=[],
    )
    out_path = tmp_path / "track.json"
    write_track_report(str(out_path), report)
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["usable"] is True
    assert payload["source"] == "detection_report"


def test_build_face_track_for_clip_uses_detection_and_tracking(tmp_path):
    detection = DetectionReport(
        ok=True,
        input_path=str(tmp_path / "in.mp4"),
        detection_fps=2.0,
        detector_backend="mediapipe",
        frames_sampled=2,
        frame_width=1000,
        frame_height=800,
        detections=[
            _det(0, x=400, y=100, w=180, h=180),
            _det(1, x=405, y=102, w=180, h=180),
        ],
    )
    expected_track = build_primary_face_track(detection)

    with patch("reframing.tracker.detect_faces_for_clip", return_value=detection):
        det_report, track_report = build_face_track_for_clip(
            str(tmp_path / "in.mp4"),
            tmp_dir=str(tmp_path),
        )

    assert det_report is detection
    assert track_report.usable == expected_track.usable
    assert track_report.track_samples == expected_track.track_samples
