from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from .models import PreflightResult, SourceClip


def preferred_media_path(clip: SourceClip | dict[str, Any]) -> str | None:
    if isinstance(clip, SourceClip):
        candidates = (clip.job_clip_path, clip.clip_path)
    else:
        candidates = (clip.get("rendered_asset_path"), clip.get("job_clip_path"), clip.get("clip_path"))
    for raw in candidates:
        if isinstance(raw, str) and raw.strip():
            return os.path.abspath(os.path.expanduser(raw.strip()))
    return None


def ffprobe_duration_sec(path: str, *, timeout_sec: int = 30) -> float | None:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "{}")
        return float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def run_preflight(
    clip: SourceClip,
    *,
    duration_tolerance_sec: float = 1.0,
    require_validation: bool = True,
) -> PreflightResult:
    issues: list[str] = []
    media_path = preferred_media_path(clip)
    file_size: int | None = None
    probed_duration: float | None = None
    expected_duration = clip.duration_sec

    if not media_path:
        issues.append("missing_media_path")
    elif not os.path.isfile(media_path):
        issues.append("media_file_missing")
    else:
        file_size = os.path.getsize(media_path)
        if file_size <= 0:
            issues.append("media_file_empty")
        probed_duration = ffprobe_duration_sec(media_path)
        if probed_duration is None:
            issues.append("ffprobe_duration_unavailable")

    if expected_duration is None:
        issues.append("missing_expected_duration")
    elif probed_duration is not None:
        if abs(float(probed_duration) - float(expected_duration)) > float(duration_tolerance_sec):
            issues.append("duration_tolerance_exceeded")

    validation = clip.clip_validation if isinstance(clip.clip_validation, dict) else {}
    if require_validation and validation.get("ok") is not True:
        issues.append("clip_validation_not_ok")

    if not clip.clip_id:
        issues.append("missing_clip_id")
    if not clip.start or not clip.end:
        issues.append("missing_timestamps")
    if not (clip.title or clip.hook):
        issues.append("missing_title_or_hook")
    if not clip.caption:
        issues.append("missing_caption")

    return PreflightResult(
        ok=not issues,
        media_path=media_path,
        file_size_bytes=file_size,
        ffprobe_duration_sec=probed_duration,
        expected_duration_sec=expected_duration,
        issues=issues,
    )
