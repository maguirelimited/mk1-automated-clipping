"""Validate a downloaded media file using ffprobe.

Validation rules (mk1):
* file exists
* file size > 0
* ffprobe can read the duration
* duration fits funnel min/max
* at least one video stream and one audio stream are present
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .funnel_loader import Funnel


log = logging.getLogger(__name__)


class ValidationError(Exception):
    pass


@dataclass
class ValidationResult:
    ok: bool
    duration_seconds: float
    has_video: bool
    has_audio: bool
    detail: str = ""


def _ffprobe_path() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise ValidationError(
            "ffprobe is not installed or not on PATH; install ffmpeg to validate media."
        )
    return path


def _probe(file: Path) -> dict:
    cmd = [
        _ffprobe_path(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file),
    ]
    try:
        out = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        raise ValidationError(f"ffprobe failed: {exc.stderr.strip() or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValidationError("ffprobe timed out after 120s") from exc

    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"ffprobe output was not valid JSON: {exc}") from exc


def validate_media(file: Path, funnel: Funnel) -> ValidationResult:
    """Return a ``ValidationResult``. Raises ``ValidationError`` for hard failures."""
    if not file.exists():
        raise ValidationError(f"File does not exist: {file}")
    if file.stat().st_size <= 0:
        raise ValidationError(f"File is empty: {file}")

    info = _probe(file)
    fmt = info.get("format") or {}
    streams = info.get("streams") or []

    duration_str = fmt.get("duration")
    try:
        duration = float(duration_str) if duration_str is not None else 0.0
    except (TypeError, ValueError):
        duration = 0.0

    if duration <= 0:
        raise ValidationError("Could not read a positive duration from the file.")

    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    if not has_video:
        raise ValidationError("File has no video stream.")
    if not has_audio:
        raise ValidationError("File has no audio stream.")

    if duration < funnel.min_duration_seconds:
        raise ValidationError(
            f"Duration {duration:.1f}s is below funnel minimum {funnel.min_duration_seconds}s."
        )
    if duration > funnel.max_duration_seconds:
        raise ValidationError(
            f"Duration {duration:.1f}s is above funnel maximum {funnel.max_duration_seconds}s."
        )

    return ValidationResult(
        ok=True,
        duration_seconds=duration,
        has_video=has_video,
        has_audio=has_audio,
        detail="ok",
    )
