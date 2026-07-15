"""Focused tests for reframing.crop_path_planner — 9:16 crop path planning."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from reframing.crop_path_planner import (
    REASON_TRACK_NOT_USABLE,
    build_crop_path_from_track,
    build_face_crop_path_for_clip,
    compute_max_crop_dimensions,
    position_crop_for_face,
    write_crop_path_report,
)
from reframing.types import BoundingBox, CropPathReport, TrackReport, TrackedFaceSample


def _bbox(x: int, y: int, w: int, h: int) -> BoundingBox:
    return BoundingBox(x=x, y=y, width=w, height=h)


def _track_sample(
    frame_index: int,
    *,
    x: int,
    y: int,
    w: int,
    h: int,
    held: bool = False,
    missing: bool = False,
    fps: float = 2.0,
) -> TrackedFaceSample:
    return TrackedFaceSample(
        timestamp_sec=frame_index / fps,
        frame_index=frame_index,
        bbox=_bbox(x, y, w, h),
        confidence=0.9,
        missing=missing,
        held=held,
    )


def _usable_track(
    samples: list[TrackedFaceSample],
    *,
    frames_sampled: int | None = None,
) -> TrackReport:
    observed = sum(1 for s in samples if not s.missing)
    sampled = frames_sampled if frames_sampled is not None else len(samples)
    coverage = (observed / sampled * 100.0) if sampled else 0.0
    return TrackReport(
        ok=True,
        usable=True,
        input_path="/in.mp4",
        frames_sampled=sampled,
        track_samples=observed,
        track_coverage_pct=coverage,
        track=samples,
    )


def test_unusable_track_returns_unusable_crop_path():
    report = build_crop_path_from_track(
        TrackReport(ok=True, usable=False, input_path="/in.mp4"),
        source_width=1920,
        source_height=1080,
    )
    assert report.ok is False
    assert report.usable is False
    assert report.reason == REASON_TRACK_NOT_USABLE


def test_landscape_source_crop_dimensions():
    crop_w, crop_h, err = compute_max_crop_dimensions(1920, 1080)
    assert err is None
    assert crop_h == 1080
    assert crop_w == 608


def test_vertical_source_full_frame_crop():
    crop_w, crop_h, err = compute_max_crop_dimensions(1080, 1920)
    assert err is None
    assert crop_w == 1080
    assert crop_h == 1920


def test_square_source_valid_crop():
    crop_w, crop_h, err = compute_max_crop_dimensions(1000, 1000)
    assert err is None
    assert crop_h == 1000
    assert crop_w == 562


def test_crop_follows_face_x_position():
    track = _usable_track([
        _track_sample(0, x=400, y=100, w=180, h=180),
        _track_sample(1, x=900, y=100, w=180, h=180),
    ])
    report = build_crop_path_from_track(track, source_width=1920, source_height=1080)
    assert report.usable is True
    assert report.samples[0].crop.x < report.samples[1].crop.x


def test_crop_clamps_at_left_edge():
    track = _usable_track([_track_sample(0, x=20, y=100, w=80, h=80)])
    report = build_crop_path_from_track(track, source_width=1920, source_height=1080)
    assert report.samples[0].crop.x == 0


def test_crop_clamps_at_right_edge():
    track = _usable_track([_track_sample(0, x=1850, y=100, w=60, h=80)])
    report = build_crop_path_from_track(track, source_width=1920, source_height=1080)
    sample = report.samples[0]
    assert sample.crop.x + sample.crop.width <= 1920


def test_upper_third_y_placement_formula():
    bbox = _bbox(820, 140, 180, 180)
    crop = position_crop_for_face(
        bbox,
        crop_width=608,
        crop_height=1080,
        source_width=1920,
        source_height=1080,
        head_vertical_ratio=0.35,
    )
    face_cy = 140 + 90
    expected_y = face_cy - (1080 * 0.35)
    assert crop.y == 0
    assert crop.x == int(round((820 + 90) - (608 / 2.0)))


def test_crop_clamps_top_and_bottom_when_source_taller_than_crop():
    track = _usable_track([_track_sample(0, x=200, y=900, w=120, h=120)])
    report = build_crop_path_from_track(
        track,
        source_width=1080,
        source_height=1920,
        config={"head_vertical_ratio": 0.35},
    )
    sample = report.samples[0]
    assert sample.crop.y >= 0
    assert sample.crop.y + sample.crop.height <= 1920


def test_held_track_samples_produce_held_crop_samples():
    track = _usable_track([
        _track_sample(0, x=400, y=100, w=180, h=180),
        _track_sample(1, x=400, y=100, w=180, h=180, held=True, missing=True),
        _track_sample(2, x=410, y=104, w=180, h=180),
    ], frames_sampled=3)
    report = build_crop_path_from_track(track, source_width=1920, source_height=1080)
    assert report.samples[1].held is True


def test_vertical_source_produces_full_frame_crop_path():
    track = _usable_track([_track_sample(0, x=400, y=700, w=180, h=180)])
    report = build_crop_path_from_track(track, source_width=1080, source_height=1920)
    assert report.usable is True
    assert report.crop_width == 1080
    assert report.crop_height == 1920
    assert report.samples[0].crop.x == 0
    assert report.samples[0].crop.y == 0


def test_invalid_source_dimensions_fail_cleanly():
    report = build_crop_path_from_track(
        _usable_track([_track_sample(0, x=100, y=100, w=80, h=80)]),
        source_width=0,
        source_height=1080,
    )
    assert report.ok is False
    assert report.usable is False


def test_write_crop_path_report_json(tmp_path):
    report = CropPathReport(
        ok=True,
        usable=True,
        input_path="/in.mp4",
        source_width=1920,
        source_height=1080,
        crop_width=608,
        crop_height=1080,
        samples=[],
    )
    out_path = tmp_path / "crop_path.json"
    write_crop_path_report(str(out_path), report)
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["usable"] is True
    assert payload["crop_width"] == 608


def test_build_face_crop_path_for_clip_with_mocks(tmp_path):
    detection = type("Det", (), {"ok": True, "input_path": "/in.mp4"})()
    track = _usable_track([
        _track_sample(0, x=400, y=100, w=180, h=180),
        _track_sample(1, x=405, y=102, w=180, h=180),
        _track_sample(2, x=410, y=104, w=180, h=180),
    ])
    with patch("reframing.crop_path_planner.build_face_track_for_clip") as mock_pipeline:
        from reframing.types import DetectionReport

        det = DetectionReport(
            ok=True,
            input_path=str(tmp_path / "in.mp4"),
            detection_fps=2.0,
            detector_backend="mediapipe",
            frame_width=1920,
            frame_height=1080,
        )
        mock_pipeline.return_value = (det, track)
        det_report, track_report, crop_report = build_face_crop_path_for_clip(
            str(tmp_path / "in.mp4"),
            source_width=1920,
            source_height=1080,
            tmp_dir=str(tmp_path),
        )

    assert det_report is det
    assert track_report is track
    assert crop_report.usable is True
    assert len(crop_report.samples) == 3
