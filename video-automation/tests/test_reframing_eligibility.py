"""Focused tests for reframing.eligibility — face-track eligibility gating."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from reframing.eligibility import (  # noqa: E402
    REASON_CROP_JUMP_RISK,
    REASON_DETECTOR_UNAVAILABLE,
    REASON_ELIGIBLE,
    REASON_INSUFFICIENT_FACE_COVERAGE,
    REASON_INSUFFICIENT_SUSTAINED_FACE_RUN_PCT,
    REASON_LEADING_NO_FACE_GAP,
    REASON_LONG_NO_FACE_GAP,
    REASON_MULTI_FACE_LAYOUT_RISK,
    REASON_NO_FACES,
    REASON_NO_SUSTAINED_FACE_VISIBLE_RUN,
    REASON_TRAILING_NO_FACE_GAP,
    REASON_UNSTABLE_CROP_LAYOUT,
    evaluate_face_track_eligibility,
    face_track_eligibility_metadata,
    write_eligibility_report,
)
from reframing.tracker import REASON_EXCESSIVE_GAP, REASON_NO_DETECTIONS  # noqa: E402
from reframing.types import (  # noqa: E402
    BoundingBox,
    CropPathReport,
    CropPathSample,
    CropRect,
    DetectionReport,
    FaceDetection,
    TrackReport,
    TrackedFaceSample,
)


def _bbox(x: int, y: int, w: int = 80, h: int = 80) -> BoundingBox:
    return BoundingBox(x=x, y=y, width=w, height=h)


def _detection(
    frame_index: int,
    *,
    x: int = 100,
    confidence: float = 0.9,
) -> FaceDetection:
    return FaceDetection(
        timestamp_sec=frame_index / 2.0,
        frame_index=frame_index,
        bbox=_bbox(x, y=50),
        confidence=confidence,
    )


def _detection_report(
    *,
    ok: bool = True,
    frames_sampled: int = 20,
    frames_with_faces: int = 20,
    detections: list[FaceDetection] | None = None,
    reason: str | None = None,
    frame_width: int = 1920,
    frame_height: int = 1080,
) -> DetectionReport:
    if detections is None and ok:
        detections = [_detection(i) for i in range(frames_with_faces)]
    return DetectionReport(
        ok=ok,
        input_path="/in.mp4",
        detection_fps=2.0,
        detector_backend="mediapipe",
        frames_sampled=frames_sampled,
        frames_with_faces=frames_with_faces,
        faces_detected_pct=(frames_with_faces / frames_sampled * 100.0) if frames_sampled else 0.0,
        frame_width=frame_width,
        frame_height=frame_height,
        detections=detections or [],
        reason=reason,
    )


def _track_sample(frame_index: int, *, missing: bool = False, x: int = 100) -> TrackedFaceSample:
    return TrackedFaceSample(
        timestamp_sec=frame_index / 2.0,
        frame_index=frame_index,
        bbox=None if missing else _bbox(x, y=50),
        confidence=None if missing else 0.9,
        missing=missing,
        held=missing,
    )


def _track_report(
    *,
    ok: bool = True,
    usable: bool = True,
    frames_sampled: int = 20,
    track_samples: int = 20,
    coverage_pct: float = 100.0,
    max_gap_sec: float = 0.0,
    track: list[TrackedFaceSample] | None = None,
    reason: str | None = None,
) -> TrackReport:
    if track is None and ok:
        track = [_track_sample(i) for i in range(track_samples)]
    return TrackReport(
        ok=ok,
        usable=usable,
        input_path="/in.mp4",
        frames_sampled=frames_sampled,
        track_samples=track_samples,
        track_coverage_pct=coverage_pct,
        max_gap_sec=max_gap_sec,
        track=track or [],
        reason=reason,
    )


def _crop_sample(frame_index: int, *, x: int, width: int = 608) -> CropPathSample:
    return CropPathSample(
        timestamp_sec=frame_index / 2.0,
        frame_index=frame_index,
        crop=CropRect(x=x, y=0, width=width, height=1080),
        held=False,
    )


def _stable_crop_report(*, samples: int = 20, x: int = 800) -> CropPathReport:
    return CropPathReport(
        ok=True,
        usable=True,
        input_path="/in.mp4",
        source_width=1920,
        source_height=1080,
        crop_width=608,
        crop_height=1080,
        smoothed=True,
        samples=[_crop_sample(i, x=x + (i % 2)) for i in range(samples)],
    )


def _unstable_crop_report(*, samples: int = 20) -> CropPathReport:
    xs = [200 if i % 2 == 0 else 1200 for i in range(samples)]
    return CropPathReport(
        ok=True,
        usable=True,
        input_path="/in.mp4",
        source_width=1920,
        source_height=1080,
        crop_width=608,
        crop_height=1080,
        smoothed=True,
        samples=[_crop_sample(i, x=x) for i, x in enumerate(xs)],
    )


def _jumping_crop_report(*, samples: int = 10) -> CropPathReport:
    xs = [800] * (samples - 1) + [800 + 200]
    return CropPathReport(
        ok=True,
        usable=True,
        input_path="/in.mp4",
        source_width=1920,
        source_height=1080,
        crop_width=608,
        crop_height=1080,
        smoothed=True,
        samples=[_crop_sample(i, x=x) for i, x in enumerate(xs)],
    )


def test_detector_unavailable_is_ineligible():
    report = evaluate_face_track_eligibility(
        _detection_report(ok=False, reason="detector_dependency_unavailable"),
        _track_report(ok=False, usable=False, reason="detection_failed"),
    )
    assert report.eligible is False
    assert report.reason == REASON_DETECTOR_UNAVAILABLE


def test_no_detections_is_ineligible():
    report = evaluate_face_track_eligibility(
        _detection_report(frames_sampled=10, frames_with_faces=0, detections=[]),
        _track_report(
            ok=False,
            usable=False,
            frames_sampled=10,
            track_samples=0,
            coverage_pct=0.0,
            reason=REASON_NO_DETECTIONS,
        ),
    )
    assert report.eligible is False
    assert report.reason == REASON_NO_FACES


def test_low_face_coverage_is_ineligible():
    report = evaluate_face_track_eligibility(
        _detection_report(frames_sampled=100, frames_with_faces=30),
        _track_report(
            frames_sampled=100,
            track_samples=30,
            coverage_pct=30.0,
            track=[_track_sample(i) if i < 30 else _track_sample(i, missing=True) for i in range(100)],
        ),
    )
    assert report.eligible is False
    assert report.reason == REASON_INSUFFICIENT_FACE_COVERAGE


def test_long_no_face_gap_is_ineligible():
    track = [_track_sample(i, missing=True) for i in range(80)] + [
        _track_sample(i) for i in range(80, 100)
    ]
    report = evaluate_face_track_eligibility(
        _detection_report(frames_sampled=100, frames_with_faces=20),
        _track_report(
            frames_sampled=100,
            track_samples=20,
            coverage_pct=20.0,
            max_gap_sec=40.0,
            track=track,
        ),
    )
    assert report.eligible is False
    assert report.reason in {REASON_INSUFFICIENT_FACE_COVERAGE, REASON_LONG_NO_FACE_GAP}


def test_no_sustained_face_visible_run_is_ineligible():
    track = [_track_sample(i) for i in range(10)]
    report = evaluate_face_track_eligibility(
        _detection_report(frames_sampled=10, frames_with_faces=10),
        _track_report(
            frames_sampled=10,
            track_samples=10,
            coverage_pct=100.0,
            track=track,
        ),
        config={"min_longest_face_run_sec": 8.0},
    )
    assert report.eligible is False
    assert report.reason == REASON_NO_SUSTAINED_FACE_VISIBLE_RUN


def test_clean_solo_track_is_eligible():
    report = evaluate_face_track_eligibility(
        _detection_report(),
        _track_report(),
        smoothed_crop_path_report=_stable_crop_report(),
        clip_duration_sec=10.0,
    )
    assert report.eligible is True
    assert report.reason == REASON_ELIGIBLE
    assert report.track_usable is True
    assert report.layout_risk is False


def test_leading_no_face_gap_is_ineligible():
    track = [_track_sample(i, missing=True) for i in range(3)] + [
        _track_sample(i) for i in range(3, 20)
    ]
    report = evaluate_face_track_eligibility(
        _detection_report(frames_sampled=20, frames_with_faces=17),
        _track_report(
            frames_sampled=20,
            track_samples=17,
            coverage_pct=85.0,
            track=track,
        ),
        clip_duration_sec=10.0,
    )
    assert report.eligible is False
    assert report.reason == REASON_LEADING_NO_FACE_GAP
    assert report.leading_no_face_gap_sec == 1.5


def test_trailing_no_face_gap_is_ineligible():
    track = [_track_sample(i) for i in range(14)] + [
        _track_sample(i, missing=True) for i in range(14, 20)
    ]
    report = evaluate_face_track_eligibility(
        _detection_report(frames_sampled=20, frames_with_faces=14),
        _track_report(
            frames_sampled=20,
            track_samples=14,
            coverage_pct=70.0,
            track=track,
        ),
        clip_duration_sec=10.0,
    )
    assert report.eligible is False
    assert report.reason == REASON_TRAILING_NO_FACE_GAP


def test_insufficient_sustained_face_run_pct_is_ineligible():
    track = [_track_sample(i) for i in range(8)] + [
        _track_sample(i, missing=True) for i in range(8, 10)
    ] + [_track_sample(i) for i in range(10, 18)] + [
        _track_sample(i, missing=True) for i in range(18, 20)
    ]
    report = evaluate_face_track_eligibility(
        _detection_report(frames_sampled=20, frames_with_faces=16),
        _track_report(
            frames_sampled=20,
            track_samples=16,
            coverage_pct=80.0,
            track=track,
        ),
        clip_duration_sec=10.0,
        config={"min_longest_face_run_sec": 3.0},
    )
    assert report.eligible is False
    assert report.reason == REASON_INSUFFICIENT_SUSTAINED_FACE_RUN_PCT


def test_high_coverage_with_bad_leading_gap_is_ineligible():
    track = [_track_sample(i, missing=True) for i in range(4)] + [
        _track_sample(i) for i in range(4, 40)
    ]
    report = evaluate_face_track_eligibility(
        _detection_report(frames_sampled=40, frames_with_faces=36),
        _track_report(
            frames_sampled=40,
            track_samples=36,
            coverage_pct=90.0,
            track=track,
        ),
        clip_duration_sec=20.0,
    )
    assert report.eligible is False
    assert report.reason == REASON_LEADING_NO_FACE_GAP


def test_unstable_crop_travel_is_ineligible():
    report = evaluate_face_track_eligibility(
        _detection_report(),
        _track_report(),
        smoothed_crop_path_report=_unstable_crop_report(),
        clip_duration_sec=10.0,
    )
    assert report.eligible is False
    assert report.reason == REASON_UNSTABLE_CROP_LAYOUT
    assert report.layout_risk is True


def test_large_adjacent_crop_jump_is_ineligible():
    report = evaluate_face_track_eligibility(
        _detection_report(),
        _track_report(),
        smoothed_crop_path_report=_jumping_crop_report(samples=20),
        clip_duration_sec=10.0,
    )
    assert report.eligible is False
    assert report.reason == REASON_CROP_JUMP_RISK


def test_eligibility_works_without_crop_path():
    report = evaluate_face_track_eligibility(
        _detection_report(),
        _track_report(),
        clip_duration_sec=10.0,
    )
    assert report.eligible is True
    assert report.metrics["crop_layout_evaluated"] is False


def test_crop_path_metrics_included_when_present():
    report = evaluate_face_track_eligibility(
        _detection_report(),
        _track_report(),
        smoothed_crop_path_report=_stable_crop_report(),
        clip_duration_sec=10.0,
    )
    assert report.metrics["crop_layout_evaluated"] is True
    assert report.crop_x_range_px >= 0


def test_clean_solo_track_is_eligible_without_crop_path():
    report = evaluate_face_track_eligibility(
        _detection_report(),
        _track_report(),
    )
    assert report.eligible is True
    assert report.reason == REASON_ELIGIBLE
    assert report.track_usable is True


def test_track_unusable_reason_preserved_when_other_checks_pass():
    report = evaluate_face_track_eligibility(
        _detection_report(),
        _track_report(
            usable=False,
            max_gap_sec=0.0,
            reason=REASON_EXCESSIVE_GAP,
        ),
        config={"max_no_face_gap_sec": 60.0, "min_face_coverage_pct": 10.0},
    )
    assert report.eligible is False
    assert report.reason == REASON_LONG_NO_FACE_GAP


def test_multi_face_layout_risk_is_ineligible():
    detections = []
    for frame_index in range(20):
        detections.append(_detection(frame_index, x=200))
        detections.append(
            FaceDetection(
                timestamp_sec=frame_index / 2.0,
                frame_index=frame_index,
                bbox=_bbox(x=1400, y=50),
                confidence=0.9,
            )
        )
    report = evaluate_face_track_eligibility(
        _detection_report(
            frames_sampled=20,
            frames_with_faces=20,
            detections=detections,
        ),
        _track_report(frames_sampled=20, track_samples=20),
    )
    assert report.eligible is False
    assert report.reason == REASON_MULTI_FACE_LAYOUT_RISK
    assert report.multi_face_risk is True


def test_eligibility_metadata_success():
    from reframing.types import FaceTrackEligibilityReport

    report = FaceTrackEligibilityReport(
        ok=True,
        eligible=True,
        reason=REASON_ELIGIBLE,
        face_coverage_pct=95.0,
        max_no_face_gap_sec=0.5,
        longest_face_run_sec=18.0,
        multi_face_risk=False,
        primary_track_dominance_pct=100.0,
    )
    meta = face_track_eligibility_metadata(report, reframe_mode="auto", use_blur_fallback=True)
    assert meta["face_track_eligible"] is True
    assert meta["face_track_eligibility_reason"] == REASON_ELIGIBLE
    assert "face_track_eligibility_fallback" not in meta


def test_eligibility_metadata_failure_in_auto_includes_fallback():
    from reframing.types import FaceTrackEligibilityReport

    report = FaceTrackEligibilityReport(
        ok=True,
        eligible=False,
        reason=REASON_LONG_NO_FACE_GAP,
        face_coverage_pct=31.67,
        max_no_face_gap_sec=39.5,
        longest_face_run_sec=18.5,
    )
    meta = face_track_eligibility_metadata(report, reframe_mode="auto", use_blur_fallback=True)
    assert meta["face_track_eligible"] is False
    assert meta["face_track_eligibility_reason"] == REASON_LONG_NO_FACE_GAP
    assert meta["face_track_eligibility_fallback"] == "blur_background"


def test_sidecar_json_serialisation(tmp_path):
    report = evaluate_face_track_eligibility(
        _detection_report(frames_sampled=100, frames_with_faces=30),
        _track_report(
            frames_sampled=100,
            track_samples=30,
            coverage_pct=30.0,
            track=[_track_sample(i) if i < 30 else _track_sample(i, missing=True) for i in range(100)],
        ),
    )
    path = tmp_path / "face_track_eligibility_report.json"
    write_eligibility_report(str(path), report)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["eligible"] is False
    assert payload["reason"] == REASON_INSUFFICIENT_FACE_COVERAGE
    assert payload["leading_no_face_gap_sec"] == payload["metrics"]["leading_no_face_gap_sec"]
    assert "longest_face_run_pct" in payload["metrics"]
    assert "max_leading_no_face_sec" in payload["thresholds"]
