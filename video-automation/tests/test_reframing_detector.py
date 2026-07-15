"""Focused tests for reframing.detector — frame sampling and face detection."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from reframing.detector import (
    REASON_DEPENDENCY_UNAVAILABLE,
    REASON_FRAME_SAMPLE_FAILED,
    REASON_INPUT_INVALID,
    build_sample_frames_command,
    detect_faces,
    detect_faces_for_clip,
    detector_backend_available,
    mediapipe_import_status,
    resolve_face_detector_model_path,
    sample_frames,
    write_detection_report,
    _relative_bbox_to_pixels,
)
from reframing.types import DetectionReport, FaceDetection


def test_build_sample_frames_command_uses_fps_filter():
    cmd = build_sample_frames_command(
        input_path="/in.mp4",
        frames_dir="/tmp/frames",
        detection_fps=2,
    )
    cmd_str = " ".join(cmd)
    assert "ffmpeg" in cmd
    assert "-vf fps=2" in cmd_str
    assert "/tmp/frames/reframe_frame_%06d.jpg" in cmd_str


def test_sample_frames_handles_ffmpeg_failure(tmp_path):
    input_path = tmp_path / "in.mp4"
    input_path.write_bytes(b"fake")
    frames_dir = str(tmp_path / "frames")

    with patch("reframing.detector.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        samples, error = sample_frames(str(input_path), frames_dir=frames_dir, detection_fps=2)

    assert samples == []
    assert error is not None
    assert "exited with code 1" in error


def test_sample_frames_builds_frame_samples(tmp_path):
    input_path = tmp_path / "in.mp4"
    input_path.write_bytes(b"fake")
    frames_dir = tmp_path / "frames"

    def _fake_ffmpeg(*_args, **_kwargs):
        frames_dir.mkdir(parents=True, exist_ok=True)
        (frames_dir / "reframe_frame_000001.jpg").write_bytes(b"jpeg")
        (frames_dir / "reframe_frame_000002.jpg").write_bytes(b"jpeg")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("reframing.detector.subprocess.run", side_effect=_fake_ffmpeg):
        samples, error = sample_frames(str(input_path), frames_dir=str(frames_dir), detection_fps=2)

    assert error is None
    assert len(samples) == 2
    assert samples[0].frame_index == 0
    assert samples[0].timestamp_sec == 0.0
    assert samples[1].frame_index == 1
    assert samples[1].timestamp_sec == 0.5


def test_detector_backend_unavailable_message():
    with patch("reframing.detector.mediapipe_import_status", return_value=(False, "missing")):
        available, message = detector_backend_available("mediapipe")
    assert available is False
    assert message == "missing"


def test_detect_faces_returns_unavailable_when_backend_missing(tmp_path):
    frame_path = tmp_path / "frame.jpg"
    frame_path.write_bytes(b"not-a-real-jpeg")

    with patch("reframing.detector.detector_backend_available", return_value=(False, "missing mp")):
        detections, error = detect_faces(
            str(frame_path),
            frame_width=640,
            frame_height=360,
        )

    assert detections == []
    assert error == "missing mp"


def test_relative_bbox_to_pixels_converts_normalized_values():
    bbox = _relative_bbox_to_pixels(0.25, 0.10, 0.50, 0.40, frame_width=1000, frame_height=800)
    assert bbox.x == 250
    assert bbox.y == 80
    assert bbox.width == 500
    assert bbox.height == 320


def test_relative_bbox_to_pixels_clamps_to_frame_bounds():
    bbox = _relative_bbox_to_pixels(-0.10, 0.90, 0.50, 0.50, frame_width=200, frame_height=100)
    assert bbox.x == 0
    assert bbox.y == 90
    assert bbox.x + bbox.width <= 200
    assert bbox.y + bbox.height <= 100


def test_detect_faces_for_clip_invalid_input_path():
    report = detect_faces_for_clip("", tmp_dir="/tmp")
    assert report.ok is False
    assert report.reason == REASON_INPUT_INVALID


def test_detect_faces_for_clip_missing_file(tmp_path):
    report = detect_faces_for_clip(
        str(tmp_path / "missing.mp4"),
        tmp_dir=str(tmp_path),
    )
    assert report.ok is False
    assert report.reason == REASON_INPUT_INVALID


def test_detect_faces_for_clip_dependency_unavailable(tmp_path):
    input_path = tmp_path / "in.mp4"
    input_path.write_bytes(b"fake")

    with patch("reframing.detector.detector_backend_available", return_value=(False, "no mediapipe")):
        report = detect_faces_for_clip(str(input_path), tmp_dir=str(tmp_path))

    assert report.ok is False
    assert report.reason == REASON_DEPENDENCY_UNAVAILABLE
    assert "no mediapipe" in (report.message or "")


def test_detect_faces_for_clip_no_faces_returns_empty_report(tmp_path):
    input_path = tmp_path / "in.mp4"
    input_path.write_bytes(b"fake")
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    frame_path = frames_dir / "reframe_frame_000001.jpg"
    frame_path.write_bytes(b"jpeg")

    with patch("reframing.detector.detector_backend_available", return_value=(True, None)), \
         patch("reframing.detector.resolve_face_detector_model_path", return_value=("/model.tflite", None)), \
         patch("reframing.detector.sample_frames") as mock_sample, \
         patch("reframing.detector._mediapipe_face_detector") as mock_session, \
         patch("reframing.detector._detect_faces_with_tasks_detector", return_value=([], None)), \
         patch("reframing.detector._load_image_rgb", return_value=(MagicMock(), None)), \
         patch("reframing.detector._probe_frame_dimensions", return_value=(640, 360, None)):
        from reframing.types import FrameSample

        mock_session.return_value.__enter__.return_value = MagicMock()
        mock_sample.return_value = (
            [FrameSample(timestamp_sec=0.0, frame_path=str(frame_path), frame_index=0)],
            None,
        )
        report = detect_faces_for_clip(str(input_path), tmp_dir=str(tmp_path))

    assert report.ok is True
    assert report.frames_sampled == 1
    assert report.frames_with_faces == 0
    assert report.faces_detected_pct == 0.0
    assert report.detections == []


def test_detect_faces_for_clip_frame_sample_failure(tmp_path):
    input_path = tmp_path / "in.mp4"
    input_path.write_bytes(b"fake")

    with patch("reframing.detector.detector_backend_available", return_value=(True, None)), \
         patch("reframing.detector.sample_frames", return_value=([], "ffmpeg failed")):
        report = detect_faces_for_clip(str(input_path), tmp_dir=str(tmp_path))

    assert report.ok is False
    assert report.reason == REASON_FRAME_SAMPLE_FAILED


def test_write_detection_report_json(tmp_path):
    report = DetectionReport(
        ok=True,
        input_path="/in.mp4",
        detection_fps=2.0,
        detector_backend="mediapipe",
        frames_sampled=2,
        frames_with_faces=1,
        faces_detected_pct=50.0,
        frame_width=640,
        frame_height=360,
        detections=[
            FaceDetection(
                timestamp_sec=0.5,
                bbox=_relative_bbox_to_pixels(0.1, 0.2, 0.3, 0.4, frame_width=640, frame_height=360),
                confidence=0.91,
                frame_index=1,
            )
        ],
    )
    out_path = tmp_path / "detection.json"
    write_detection_report(str(out_path), report)

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["frames_sampled"] == 2
    assert payload["faces_detected_pct"] == 50.0
    assert payload["detections"][0]["confidence"] == 0.91
    assert payload["detections"][0]["bbox"]["width"] > 0


def test_detect_faces_mediapipe_tasks_converts_pixel_boxes(tmp_path):
    frame_path = tmp_path / "frame.jpg"
    frame_path.write_bytes(b"jpeg")
    expected_bbox = _relative_bbox_to_pixels(0.2, 0.1, 0.5, 0.4, frame_width=1000, frame_height=800)

    with patch("reframing.detector.detector_backend_available", return_value=(True, None)), \
         patch("reframing.detector.resolve_face_detector_model_path", return_value=("/model.tflite", None)), \
         patch("reframing.detector._mediapipe_face_detector") as mock_session, \
         patch(
             "reframing.detector._detect_faces_with_tasks_detector",
             return_value=([(expected_bbox, 0.91)], None),
         ) as mock_detect, \
         patch("reframing.detector._load_image_rgb", return_value=(MagicMock(), None)):
        mock_session.return_value.__enter__.return_value = MagicMock()
        detections, error = detect_faces(
            str(frame_path),
            frame_width=1000,
            frame_height=800,
            min_confidence=0.5,
        )

    assert error is None
    assert len(detections) == 1
    bbox, confidence = detections[0]
    assert bbox.x == 200
    assert bbox.y == 80
    assert confidence == 0.91
    mock_detect.assert_called_once()


def test_mediapipe_import_status_requires_tasks_api_not_legacy_solutions():
    fake_mp = SimpleNamespace(tasks=object())
    with patch.dict(sys.modules, {"mediapipe": fake_mp}), \
         patch("reframing.detector.resolve_face_detector_model_path", return_value=("/model.tflite", None)), \
         patch("reframing.detector._mediapipe_face_detector"):
        available, message = mediapipe_import_status()
    assert available is True
    assert message is None


def test_mediapipe_import_status_fails_when_tasks_api_missing():
    fake_mp = SimpleNamespace()
    with patch.dict(sys.modules, {"mediapipe": fake_mp}):
        available, message = mediapipe_import_status()
    assert available is False
    assert message is not None
    assert "Tasks API" in message


def test_resolve_face_detector_model_path_uses_bundled_model():
    path, error = resolve_face_detector_model_path()
    assert error is None
    assert path is not None
    assert path.endswith("blaze_face_short_range.tflite")
    assert os.path.isfile(path)


@pytest.mark.skipif(
    not detector_backend_available("mediapipe")[0],
    reason="MediaPipe not installed — optional integration test skipped",
)
def test_optional_mediapipe_import_when_installed():
    available, message = mediapipe_import_status()
    assert available is True
    assert message is None


@pytest.mark.skipif(
    not detector_backend_available("mediapipe")[0],
    reason="MediaPipe not installed — optional integration test skipped",
)
def test_detect_faces_for_clip_ok_with_installed_mediapipe_tasks_api(tmp_path):
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")

    input_path = tmp_path / "in.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=30",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            input_path,
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )

    report = detect_faces_for_clip(str(input_path), tmp_dir=str(tmp_path / "work"))
    assert report.ok is True
    assert report.reason is None
    assert "solutions" not in (report.message or "")
    assert report.frames_sampled >= 1
