"""Frame sampling and face detection for future face-track crop reframing."""

from __future__ import annotations

import glob
import json
import math
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from reframing.types import BoundingBox, DetectionReport, FaceDetection, FrameSample

# ---------------------------------------------------------------------------
# Defaults (internal — not exposed in Ops UI yet)
# ---------------------------------------------------------------------------

DEFAULT_DETECTION_FPS = 2.0
DEFAULT_MIN_FACE_CONFIDENCE = 0.5
DEFAULT_DETECTOR_BACKEND = "mediapipe"
DEFAULT_MAX_SAMPLED_FRAMES = 300
DEFAULT_FACE_DETECTOR_MODEL_FILENAME = "blaze_face_short_range.tflite"

FFMPEG_SAMPLE_TIMEOUT_SEC = 120

REASON_DEPENDENCY_UNAVAILABLE = "detector_dependency_unavailable"
REASON_DETECTOR_MODEL_UNAVAILABLE = "detector_model_unavailable"
REASON_INPUT_INVALID = "detector_input_invalid"
REASON_FRAME_SAMPLE_FAILED = "frame_sample_failed"
REASON_UNSUPPORTED_BACKEND = "unsupported_detector_backend"

_FRAME_GLOB = "reframe_frame_*.jpg"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def merge_detector_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge caller overrides with detector defaults."""
    merged = {
        "detection_fps": DEFAULT_DETECTION_FPS,
        "min_face_confidence": DEFAULT_MIN_FACE_CONFIDENCE,
        "detector_backend": DEFAULT_DETECTOR_BACKEND,
        "max_sampled_frames": DEFAULT_MAX_SAMPLED_FRAMES,
    }
    if config:
        merged.update(config)
    return merged


# ---------------------------------------------------------------------------
# Dependency availability
# ---------------------------------------------------------------------------


def mediapipe_import_status() -> tuple[bool, str | None]:
    """Return whether the MediaPipe Tasks face detector can run."""
    try:
        import mediapipe as mp
    except Exception as exc:
        return False, (
            "MediaPipe is not installed or not compatible with this Python "
            f"environment ({exc!r})."
        )

    if not hasattr(mp, "tasks"):
        return False, (
            "MediaPipe is installed but the Tasks API is unavailable. "
            "Install a compatible mediapipe version from "
            "requirements-reframing-optional.txt."
        )

    model_path, model_error = resolve_face_detector_model_path()
    if model_error:
        return False, model_error

    try:
        with _mediapipe_face_detector(
            DEFAULT_MIN_FACE_CONFIDENCE,
            model_path=model_path,
        ):
            pass
    except Exception as exc:
        return False, f"MediaPipe FaceDetector could not be initialized ({exc!r})."

    return True, None


def resolve_face_detector_model_path() -> tuple[str | None, str | None]:
    """Resolve the bundled BlazeFace short-range model path."""
    env_override = os.environ.get("MK1_FACE_DETECTOR_MODEL", "").strip()
    if env_override:
        if os.path.isfile(env_override):
            return env_override, None
        return None, f"MK1_FACE_DETECTOR_MODEL does not exist: {env_override}"

    bundled = (
        Path(__file__).resolve().parents[2]
        / "models"
        / DEFAULT_FACE_DETECTOR_MODEL_FILENAME
    )
    if bundled.is_file():
        return str(bundled), None

    return (
        None,
        "Face detector model is missing. Expected bundled model at "
        f"{bundled}.",
    )


def detector_backend_available(backend: str) -> tuple[bool, str | None]:
    """Check whether the requested detector backend is usable."""
    if backend != DEFAULT_DETECTOR_BACKEND:
        return False, f"unsupported detector backend: {backend!r}"
    return mediapipe_import_status()


# ---------------------------------------------------------------------------
# Frame sampling (FFmpeg)
# ---------------------------------------------------------------------------


def build_sample_frames_command(
    *,
    input_path: str,
    frames_dir: str,
    detection_fps: float,
) -> list[str]:
    """Build ffmpeg args to sample JPEG frames at a fixed FPS."""
    output_pattern = os.path.join(frames_dir, "reframe_frame_%06d.jpg")
    fps_value = _format_fps(detection_fps)
    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_path,
        "-vf",
        f"fps={fps_value}",
        "-q:v",
        "2",
        output_pattern,
    ]


def sample_frames(
    input_path: str,
    *,
    frames_dir: str,
    detection_fps: float = DEFAULT_DETECTION_FPS,
    max_sampled_frames: int = DEFAULT_MAX_SAMPLED_FRAMES,
    timeout_sec: int = FFMPEG_SAMPLE_TIMEOUT_SEC,
) -> tuple[list[FrameSample], str | None]:
    """Sample frames from *input_path* into *frames_dir*.

    Returns ``(samples, error_message)``.  On success the error is ``None``.
    """
    os.makedirs(frames_dir, exist_ok=True)
    for stale in glob.glob(os.path.join(frames_dir, _FRAME_GLOB)):
        try:
            os.remove(stale)
        except OSError:
            pass

    cmd = build_sample_frames_command(
        input_path=input_path,
        frames_dir=frames_dir,
        detection_fps=detection_fps,
    )

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return [], f"ffmpeg frame sampling timed out after {timeout_sec}s"
    except Exception as exc:
        return [], f"ffmpeg frame sampling failed to start: {exc}"

    if proc.returncode != 0:
        stderr = ((proc.stderr or "") + (proc.stdout or "")).strip()
        tail = stderr[-800:] if stderr else "(no output)"
        return [], f"ffmpeg frame sampling exited with code {proc.returncode}: {tail}"

    frame_paths = sorted(glob.glob(os.path.join(frames_dir, _FRAME_GLOB)))
    if not frame_paths:
        return [], "ffmpeg frame sampling produced no frame files"

    if max_sampled_frames > 0:
        frame_paths = frame_paths[: int(max_sampled_frames)]

    samples: list[FrameSample] = []
    fps = float(detection_fps)
    for frame_index, frame_path in enumerate(frame_paths):
        samples.append(
            FrameSample(
                timestamp_sec=frame_index / fps,
                frame_path=frame_path,
                frame_index=frame_index,
            )
        )
    return samples, None


# ---------------------------------------------------------------------------
# Face detection
# ---------------------------------------------------------------------------


def detect_faces(
    frame_path: str,
    *,
    frame_width: int,
    frame_height: int,
    min_confidence: float = DEFAULT_MIN_FACE_CONFIDENCE,
    backend: str = DEFAULT_DETECTOR_BACKEND,
) -> tuple[list[tuple[BoundingBox, float]], str | None]:
    """Detect faces in one sampled frame.

    Returns ``(detections, error_message)`` where each detection is
    ``(bbox, confidence)``.
    """
    available, message = detector_backend_available(backend)
    if not available:
        return [], message

    image_rgb, load_error = _load_image_rgb(frame_path)
    if load_error:
        return [], load_error

    if backend == DEFAULT_DETECTOR_BACKEND:
        model_path, model_error = resolve_face_detector_model_path()
        if model_error:
            return [], model_error
        with _mediapipe_face_detector(min_confidence, model_path=model_path) as detector:
            return _detect_faces_with_tasks_detector(
                detector,
                image_rgb=image_rgb,
                frame_width=frame_width,
                frame_height=frame_height,
                min_confidence=min_confidence,
            )

    return [], f"unsupported detector backend: {backend!r}"


def detect_faces_for_clip(
    input_path: str,
    *,
    tmp_dir: str,
    config: dict[str, Any] | None = None,
    report_path: str | None = None,
) -> DetectionReport:
    """Sample frames from a clip and run face detection on each frame."""
    merged = merge_detector_config(config)
    backend = str(merged["detector_backend"])
    detection_fps = float(merged["detection_fps"])
    min_confidence = float(merged["min_face_confidence"])
    max_sampled_frames = int(merged["max_sampled_frames"])

    if not input_path or not str(input_path).strip():
        return _failure_report(
            input_path=input_path or "",
            detection_fps=detection_fps,
            detector_backend=backend,
            reason=REASON_INPUT_INVALID,
            message="input_path is missing or empty",
        )

    input_path = str(input_path)
    if not os.path.isfile(input_path):
        return _failure_report(
            input_path=input_path,
            detection_fps=detection_fps,
            detector_backend=backend,
            reason=REASON_INPUT_INVALID,
            message=f"input file does not exist: {input_path}",
        )

    available, unavailable_message = detector_backend_available(backend)
    if not available:
        return _failure_report(
            input_path=input_path,
            detection_fps=detection_fps,
            detector_backend=backend,
            reason=REASON_DEPENDENCY_UNAVAILABLE,
            message=unavailable_message or "detector dependency unavailable",
        )

    model_path, model_error = resolve_face_detector_model_path()
    if model_error:
        return _failure_report(
            input_path=input_path,
            detection_fps=detection_fps,
            detector_backend=backend,
            reason=REASON_DETECTOR_MODEL_UNAVAILABLE,
            message=model_error,
        )

    frames_dir = os.path.join(tmp_dir, "reframe_detection_frames")
    samples, sample_error = sample_frames(
        input_path,
        frames_dir=frames_dir,
        detection_fps=detection_fps,
        max_sampled_frames=max_sampled_frames,
    )
    if sample_error:
        return _failure_report(
            input_path=input_path,
            detection_fps=detection_fps,
            detector_backend=backend,
            reason=REASON_FRAME_SAMPLE_FAILED,
            message=sample_error,
        )

    frame_width, frame_height, dimension_error = _probe_frame_dimensions(samples[0].frame_path)
    if dimension_error:
        return _failure_report(
            input_path=input_path,
            detection_fps=detection_fps,
            detector_backend=backend,
            reason=REASON_FRAME_SAMPLE_FAILED,
            message=dimension_error,
            frames_sampled=len(samples),
        )

    all_detections: list[FaceDetection] = []
    frames_with_faces = 0

    with _mediapipe_face_detector(min_confidence, model_path=model_path) as detector:
        for sample in samples:
            image_rgb, load_error = _load_image_rgb(sample.frame_path)
            if load_error:
                return _failure_report(
                    input_path=input_path,
                    detection_fps=detection_fps,
                    detector_backend=backend,
                    reason=REASON_FRAME_SAMPLE_FAILED,
                    message=load_error,
                    frames_sampled=len(samples),
                    frame_width=frame_width,
                    frame_height=frame_height,
                )

            raw_faces, detect_error = _detect_faces_with_tasks_detector(
                detector,
                image_rgb=image_rgb,
                frame_width=frame_width,
                frame_height=frame_height,
                min_confidence=min_confidence,
            )
            if detect_error:
                return _failure_report(
                    input_path=input_path,
                    detection_fps=detection_fps,
                    detector_backend=backend,
                    reason=REASON_DEPENDENCY_UNAVAILABLE,
                    message=detect_error,
                    frames_sampled=len(samples),
                    frame_width=frame_width,
                    frame_height=frame_height,
                )

            if raw_faces:
                frames_with_faces += 1

            for bbox, confidence in raw_faces:
                all_detections.append(
                    FaceDetection(
                        timestamp_sec=sample.timestamp_sec,
                        bbox=bbox,
                        confidence=confidence,
                        frame_index=sample.frame_index,
                    )
                )

    faces_pct = 0.0
    if samples:
        faces_pct = (frames_with_faces / len(samples)) * 100.0

    report = DetectionReport(
        ok=True,
        input_path=input_path,
        detection_fps=detection_fps,
        detector_backend=backend,
        frames_sampled=len(samples),
        frames_with_faces=frames_with_faces,
        faces_detected_pct=faces_pct,
        frame_width=frame_width,
        frame_height=frame_height,
        detections=all_detections,
    )

    if report_path:
        write_detection_report(report_path, report)

    return report


def write_detection_report(path: str, report: DetectionReport) -> None:
    """Write a detection sidecar JSON file for debugging."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2, sort_keys=True)
        fh.write("\n")


# ---------------------------------------------------------------------------
# MediaPipe Tasks backend (0.10+)
# ---------------------------------------------------------------------------


@contextmanager
def _mediapipe_face_detector(
    min_confidence: float,
    *,
    model_path: str,
) -> Iterator[Any]:
    import mediapipe as mp
    from mediapipe.tasks import python as mp_tasks
    from mediapipe.tasks.python import vision

    options = vision.FaceDetectorOptions(
        base_options=mp_tasks.BaseOptions(model_asset_path=model_path),
        running_mode=vision.RunningMode.IMAGE,
        min_detection_confidence=min_confidence,
    )
    detector = vision.FaceDetector.create_from_options(options)
    try:
        yield detector
    finally:
        detector.close()


def _detect_faces_with_tasks_detector(
    detector: Any,
    *,
    image_rgb: Any,
    frame_width: int,
    frame_height: int,
    min_confidence: float,
) -> tuple[list[tuple[BoundingBox, float]], str | None]:
    import mediapipe as mp

    try:
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=image_rgb)
        results = detector.detect(mp_image)
    except Exception as exc:
        return [], f"MediaPipe face detection failed: {exc}"

    detections: list[tuple[BoundingBox, float]] = []
    for detection in results.detections:
        score = float(detection.categories[0].score) if detection.categories else 0.0
        if score < min_confidence:
            continue

        box = detection.bounding_box
        bbox = _clamp_bbox_to_frame(
            BoundingBox(
                x=int(box.origin_x),
                y=int(box.origin_y),
                width=int(box.width),
                height=int(box.height),
            ),
            frame_width=frame_width,
            frame_height=frame_height,
        )
        if bbox.width <= 0 or bbox.height <= 0:
            continue
        detections.append((bbox, score))

    return detections, None


def _clamp_bbox_to_frame(
    bbox: BoundingBox,
    *,
    frame_width: int,
    frame_height: int,
) -> BoundingBox:
    x = max(0, min(bbox.x, frame_width - 1))
    y = max(0, min(bbox.y, frame_height - 1))
    width = max(1, min(bbox.width, frame_width - x))
    height = max(1, min(bbox.height, frame_height - y))
    return BoundingBox(x=x, y=y, width=width, height=height)


def _relative_bbox_to_pixels(
    xmin: float,
    ymin: float,
    width: float,
    height: float,
    *,
    frame_width: int,
    frame_height: int,
) -> BoundingBox:
    """Convert MediaPipe normalized bbox to clamped pixel coordinates."""
    x = int(math.floor(xmin * frame_width))
    y = int(math.floor(ymin * frame_height))
    w = int(math.ceil(width * frame_width))
    h = int(math.ceil(height * frame_height))

    x = max(0, min(x, frame_width - 1))
    y = max(0, min(y, frame_height - 1))
    w = max(1, min(w, frame_width - x))
    h = max(1, min(h, frame_height - y))

    return BoundingBox(x=x, y=y, width=w, height=h)


# ---------------------------------------------------------------------------
# Image loading / probing
# ---------------------------------------------------------------------------


def _load_image_rgb(frame_path: str) -> tuple[Any | None, str | None]:
    try:
        from PIL import Image
    except Exception as exc:
        return None, f"Pillow is required to load sampled frames ({exc!r})"

    try:
        import numpy as np
    except Exception as exc:
        return None, f"NumPy is required to load sampled frames ({exc!r})"

    try:
        with Image.open(frame_path) as img:
            return np.asarray(img.convert("RGB")), None
    except Exception as exc:
        return None, f"could not load sampled frame {frame_path}: {exc}"


def _probe_frame_dimensions(frame_path: str) -> tuple[int, int, str | None]:
    try:
        from PIL import Image
    except Exception as exc:
        return 0, 0, f"Pillow is required to probe frame dimensions ({exc!r})"

    try:
        with Image.open(frame_path) as img:
            width, height = img.size
    except Exception as exc:
        return 0, 0, f"could not probe frame dimensions for {frame_path}: {exc}"

    if width <= 0 or height <= 0:
        return 0, 0, f"invalid frame dimensions for {frame_path}: {width}x{height}"
    return int(width), int(height), None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _failure_report(
    *,
    input_path: str,
    detection_fps: float,
    detector_backend: str,
    reason: str,
    message: str,
    frames_sampled: int = 0,
    frame_width: int | None = None,
    frame_height: int | None = None,
) -> DetectionReport:
    return DetectionReport(
        ok=False,
        input_path=input_path,
        detection_fps=detection_fps,
        detector_backend=detector_backend,
        frames_sampled=frames_sampled,
        frame_width=frame_width,
        frame_height=frame_height,
        reason=reason,
        message=message,
    )


def _format_fps(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.3f}".rstrip("0").rstrip(".")
