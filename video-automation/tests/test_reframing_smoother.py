"""Focused tests for reframing.smoother — virtual-camera crop path smoothing."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from reframing.smoother import (
    REASON_CROP_PATH_NOT_USABLE,
    SMOOTHING_METHOD,
    build_smoothed_face_crop_path_for_clip,
    smooth_crop_path,
    write_smoothed_crop_path_report,
)
from reframing.types import CropPathReport, CropPathSample, CropRect


def _crop(x: int, y: int, w: int = 608, h: int = 1080) -> CropRect:
    return CropRect(x=x, y=y, width=w, height=h)


def _sample(
    frame_index: int,
    *,
    x: int,
    y: int = 0,
    held: bool = False,
    fps: float = 2.0,
) -> CropPathSample:
    return CropPathSample(
        timestamp_sec=frame_index / fps,
        frame_index=frame_index,
        crop=_crop(x, y),
        held=held,
    )


def _usable_crop_path(
    samples: list[CropPathSample],
    *,
    source_width: int = 1920,
    source_height: int = 1080,
    crop_width: int = 608,
    crop_height: int = 1080,
) -> CropPathReport:
    return CropPathReport(
        ok=True,
        usable=True,
        input_path="/in.mp4",
        source_width=source_width,
        source_height=source_height,
        crop_width=crop_width,
        crop_height=crop_height,
        samples=samples,
    )


def test_unusable_crop_path_returns_unusable_smoothed_report():
    report = smooth_crop_path(
        CropPathReport(ok=False, usable=False, input_path="/in.mp4", reason="track_not_usable")
    )
    assert report.ok is False
    assert report.usable is False
    assert report.reason == REASON_CROP_PATH_NOT_USABLE


def test_stable_path_remains_stable():
    samples = [_sample(i, x=656) for i in range(5)]
    report = smooth_crop_path(_usable_crop_path(samples))
    assert report.usable is True
    assert report.smoothed is True
    for sample in report.samples:
        assert sample.crop.x == 656
        assert sample.crop.y == 0


def test_deadzone_removes_tiny_jitter():
    raw_xs = [654, 660, 657, 663, 659]
    samples = [_sample(i, x=x) for i, x in enumerate(raw_xs)]
    report = smooth_crop_path(
        _usable_crop_path(samples),
        config={"deadzone_px": 8, "ema_alpha": 1.0, "max_velocity_px_per_sec": 10000},
    )
    smoothed_xs = [s.crop.x for s in report.samples]
    assert smoothed_xs[0] == 654
    assert len(set(smoothed_xs[1:])) <= 2


def test_ema_reduces_movement_compared_with_raw():
    raw_xs = [500, 700, 520, 680, 540, 660]
    samples = [_sample(i, x=x) for i, x in enumerate(raw_xs)]
    raw_report = _usable_crop_path(samples)
    report = smooth_crop_path(
        raw_report,
        config={"deadzone_px": 0, "ema_alpha": 0.25, "max_velocity_px_per_sec": 10000},
    )
    assert report.movement_stats is not None
    assert report.movement_stats["smoothed_total_movement_px"] < report.movement_stats["raw_total_movement_px"]


def test_velocity_cap_prevents_sudden_large_jump():
    samples = [
        _sample(0, x=200),
        _sample(1, x=1200),
    ]
    report = smooth_crop_path(
        _usable_crop_path(samples),
        config={
            "deadzone_px": 0,
            "ema_alpha": 1.0,
            "max_velocity_px_per_sec": 900,
        },
    )
    assert report.samples[1].crop.x < 1200
    assert report.samples[1].crop.x > 200


def test_boundary_clamp_after_smoothing():
    samples = [_sample(0, x=50), _sample(1, x=50)]
    report = smooth_crop_path(
        _usable_crop_path(samples),
        config={"deadzone_px": 0, "ema_alpha": 1.0, "max_velocity_px_per_sec": 10000},
    )
    for sample in report.samples:
        crop = sample.crop
        assert crop.x >= 0
        assert crop.y >= 0
        assert crop.x + crop.width <= 1920
        assert crop.y + crop.height <= 1080


def test_held_samples_preserve_last_smoothed_crop():
    samples = [
        _sample(0, x=600),
        _sample(1, x=900, held=True),
        _sample(2, x=900),
    ]
    report = smooth_crop_path(
        _usable_crop_path(samples),
        config={"deadzone_px": 0, "ema_alpha": 0.5, "max_velocity_px_per_sec": 10000},
    )
    assert report.samples[1].held is True
    assert report.samples[1].crop.x == report.samples[0].crop.x
    assert report.samples[1].crop.y == report.samples[0].crop.y


def test_vertical_full_frame_crop_is_noop():
    samples = [_sample(i, x=0, y=0) for i in range(3)]
    report = smooth_crop_path(
        _usable_crop_path(
            samples,
            source_width=1080,
            source_height=1920,
            crop_width=1080,
            crop_height=1920,
        )
    )
    for sample in report.samples:
        assert sample.crop.x == 0
        assert sample.crop.y == 0
        assert sample.crop.width == 1080
        assert sample.crop.height == 1920


def test_movement_stats_are_calculated():
    samples = [_sample(0, x=500), _sample(1, x=700), _sample(2, x=720)]
    report = smooth_crop_path(_usable_crop_path(samples))
    assert report.movement_stats is not None
    assert report.movement_stats["raw_total_movement_px"] > 0
    assert "samples_adjusted" in report.movement_stats


def test_smoothed_report_json_serialisation(tmp_path):
    samples = [_sample(0, x=656), _sample(1, x=660)]
    report = smooth_crop_path(_usable_crop_path(samples))
    out_path = tmp_path / "smoothed_crop_path.json"
    write_smoothed_crop_path_report(str(out_path), report)
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["smoothed"] is True
    assert payload["smoothing_method"] == SMOOTHING_METHOD
    assert payload["smoothing"]["deadzone_px"] == 8
    assert payload["movement_stats"]["raw_total_movement_px"] >= 0
    assert len(payload["samples"]) == 2
    assert len(payload["raw_samples"]) == 2


def test_build_smoothed_face_crop_path_for_clip_with_mocks(tmp_path):
    from reframing.types import BoundingBox, DetectionReport, TrackReport, TrackedFaceSample

    det = DetectionReport(
        ok=True,
        input_path=str(tmp_path / "in.mp4"),
        detection_fps=2.0,
        detector_backend="mediapipe",
        frame_width=1920,
        frame_height=1080,
    )
    track = TrackReport(
        ok=True,
        usable=True,
        input_path=str(tmp_path / "in.mp4"),
        frames_sampled=3,
        track_samples=3,
        track_coverage_pct=100.0,
        track=[
            TrackedFaceSample(
                timestamp_sec=0.0,
                frame_index=0,
                bbox=BoundingBox(x=400, y=100, width=180, height=180),
                confidence=0.9,
                missing=False,
            ),
            TrackedFaceSample(
                timestamp_sec=0.5,
                frame_index=1,
                bbox=BoundingBox(x=405, y=102, width=180, height=180),
                confidence=0.9,
                missing=False,
            ),
            TrackedFaceSample(
                timestamp_sec=1.0,
                frame_index=2,
                bbox=BoundingBox(x=410, y=104, width=180, height=180),
                confidence=0.9,
                missing=False,
            ),
        ],
    )
    raw_crop = CropPathReport(
        ok=True,
        usable=True,
        input_path=str(tmp_path / "in.mp4"),
        source_width=1920,
        source_height=1080,
        crop_width=608,
        crop_height=1080,
        samples=[
            _sample(0, x=400),
            _sample(1, x=405),
            _sample(2, x=410),
        ],
    )

    with patch("reframing.smoother.build_face_crop_path_for_clip") as mock_pipeline:
        mock_pipeline.return_value = (det, track, raw_crop)
        det_report, track_report, crop_report, smoothed_report = (
            build_smoothed_face_crop_path_for_clip(
                str(tmp_path / "in.mp4"),
                source_width=1920,
                source_height=1080,
                tmp_dir=str(tmp_path),
            )
        )

    assert det_report is det
    assert track_report is track
    assert crop_report is raw_crop
    assert smoothed_report.smoothed is True
    assert len(smoothed_report.samples) == 3
