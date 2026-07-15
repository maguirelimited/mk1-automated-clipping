"""render_clip_v1 — first module in the fixed MK1 universal conveyor.

Cuts the selected candidate's timestamp range from the source video and
writes an initial rendered clip file for downstream modules to consume.

This module deliberately does NOT:
- perform 9:16 formatting or crop/pad/scale operations
- generate captions or burn subtitles
- run full validation (that is validation_v1's responsibility)
- write per-clip metadata files
- call AI/LLM services
- register output funnels
- perform audio normalisation, silence trimming, or hook restructuring

Re-encoding (not stream copy) is used by default for timestamp accuracy.
Stream copy can be enabled via config but is off by default because it
cuts only on keyframes which causes timestamp drift.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
from typing import Any

from mk04_utils import ffprobe_duration_sec
from post_processing_modules import (
    PostProcessingModule,
    make_module_fail_result,
    make_module_pass_result,
)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

MODULE_NAME = "render_clip_v1"
MODULE_VERSION = "1.0"

FFMPEG_TIMEOUT_SEC = 120
FFPROBE_TIMEOUT_SEC = 30

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict[str, Any] = {
    "output_ext": ".mp4",
    "min_duration_sec": 1.0,
    "max_duration_sec": 180.0,
    "duration_tolerance_sec": 1.0,
    "ffmpeg_preset": "veryfast",
    "video_codec": "libx264",
    "audio_codec": "aac",
    "overwrite": True,
    "copy_streams": False,
}

# ---------------------------------------------------------------------------
# Public module class
# ---------------------------------------------------------------------------


class RenderClipV1Module(PostProcessingModule):
    """Real MK1 render module — cuts the selected candidate from the source video.

    Plugs directly into :func:`run_module_chain` and
    :func:`run_fixed_mk1_universal_conveyor`.

    The module receives the source video path as ``input_path`` (preferred)
    or falls back to ``context["source_video_path"]``.  Candidate timestamps
    are read from ``context["selected_candidate"]``.
    """

    module_name = MODULE_NAME
    module_version = MODULE_VERSION

    def run(
        self,
        context: dict[str, Any],
        *,
        input_path: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged_config = {**_DEFAULT_CONFIG, **(config or {}), **(context.get("config") or {})}

        # ------------------------------------------------------------------
        # 1. Resolve and validate source video path
        # ------------------------------------------------------------------
        source_video = input_path or context.get("source_video_path")
        if not source_video or not str(source_video).strip():
            return _fail(
                "missing_source_video",
                "source video path is missing from input_path and context",
                candidate_id=None,
                input_path=input_path,
            )
        source_video = str(source_video)

        if not os.path.exists(source_video):
            return _fail(
                "source_video_not_found",
                f"source video does not exist: {source_video}",
                candidate_id=None,
                input_path=source_video,
            )
        if not os.path.isfile(source_video):
            return _fail(
                "source_video_not_file",
                f"source video path is not a regular file: {source_video}",
                candidate_id=None,
                input_path=source_video,
            )

        # ------------------------------------------------------------------
        # 2. Resolve selected candidate
        # ------------------------------------------------------------------
        selected = context.get("selected_candidate")
        if not isinstance(selected, dict) or not selected:
            return _fail(
                "missing_selected_candidate",
                "context is missing a valid selected_candidate dict",
                candidate_id=None,
                input_path=source_video,
            )

        candidate_id: str | None = selected.get("candidate_id")
        if not candidate_id or not str(candidate_id).strip():
            return _fail(
                "missing_candidate_id",
                "selected_candidate is missing a non-empty candidate_id",
                candidate_id=None,
                input_path=source_video,
            )

        # ------------------------------------------------------------------
        # 3. Validate timestamps
        # ------------------------------------------------------------------
        start_sec = selected.get("start_sec")
        end_sec = selected.get("end_sec")

        ts_error = _validate_timestamps(start_sec, end_sec, candidate_id, merged_config)
        if ts_error is not None:
            return ts_error._replace_input(source_video)

        start_sec = float(start_sec)  # type: ignore[arg-type]
        end_sec = float(end_sec)  # type: ignore[arg-type]
        expected_duration = end_sec - start_sec

        # ------------------------------------------------------------------
        # 4. Validate config
        # ------------------------------------------------------------------
        config_error = _validate_render_config(merged_config)
        if config_error:
            return _fail(
                "invalid_render_config",
                config_error,
                candidate_id=candidate_id,
                input_path=source_video,
            )

        # ------------------------------------------------------------------
        # 5. Resolve output path
        # ------------------------------------------------------------------
        job_id: str = str(context.get("job_id") or "job_unknown")
        clip_dir: str | None = context.get("clip_dir")
        if not clip_dir:
            clip_dir = os.path.join(os.path.dirname(source_video), "clips")

        output_path = _make_output_path(
            clip_dir,
            job_id,
            candidate_id,
            ext=str(merged_config.get("output_ext", ".mp4")),
        )

        # Overwrite / existence check
        overwrite = bool(merged_config.get("overwrite", True))
        if os.path.exists(output_path) and not overwrite:
            return _fail(
                "output_already_exists",
                f"output file already exists and overwrite=false: {output_path}",
                candidate_id=candidate_id,
                input_path=source_video,
            )

        # Create output directory
        try:
            os.makedirs(clip_dir, exist_ok=True)
        except OSError as exc:
            return _fail(
                "clip_dir_creation_failed",
                f"could not create clip directory: {exc}",
                candidate_id=candidate_id,
                input_path=source_video,
            )

        # ------------------------------------------------------------------
        # 6. Run ffmpeg
        # ------------------------------------------------------------------
        ffmpeg_cmd = _build_ffmpeg_command(
            source_video=source_video,
            output_path=output_path,
            start_sec=start_sec,
            duration_sec=expected_duration,
            config=merged_config,
        )
        cmd_summary = " ".join(str(a) for a in ffmpeg_cmd)

        try:
            proc = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                timeout=FFMPEG_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return _fail(
                "ffmpeg_failed",
                f"ffmpeg timed out after {FFMPEG_TIMEOUT_SEC}s",
                candidate_id=candidate_id,
                input_path=source_video,
                ffmpeg_returncode=None,
                ffmpeg_stderr_tail="timeout",
                ffmpeg_command_summary=cmd_summary,
            )
        except Exception as exc:
            return _fail(
                "unexpected_render_error",
                f"unexpected error launching ffmpeg: {exc}",
                candidate_id=candidate_id,
                input_path=source_video,
                ffmpeg_command_summary=cmd_summary,
            )

        if proc.returncode != 0:
            stderr_tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-800:]
            return _fail(
                "ffmpeg_failed",
                f"ffmpeg exited with code {proc.returncode}",
                candidate_id=candidate_id,
                input_path=source_video,
                ffmpeg_returncode=proc.returncode,
                ffmpeg_stderr_tail=stderr_tail or "(no output)",
                ffmpeg_command_summary=cmd_summary,
            )

        # ------------------------------------------------------------------
        # 7. Verify output file
        # ------------------------------------------------------------------
        if not os.path.isfile(output_path):
            return _fail(
                "output_missing",
                f"ffmpeg succeeded but output file is absent: {output_path}",
                candidate_id=candidate_id,
                input_path=source_video,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        output_size = os.path.getsize(output_path)
        if output_size == 0:
            return _fail(
                "output_empty",
                f"output file exists but is empty: {output_path}",
                candidate_id=candidate_id,
                input_path=source_video,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        # ------------------------------------------------------------------
        # 8. Probe output duration
        # ------------------------------------------------------------------
        raw_duration = ffprobe_duration_sec(output_path, timeout_sec=FFPROBE_TIMEOUT_SEC)
        actual_duration = (
            raw_duration
            if raw_duration is not None
            and math.isfinite(raw_duration)
            and raw_duration > 0
            else None
        )
        if actual_duration is None:
            return _fail(
                "duration_probe_failed",
                f"ffprobe could not read a duration from: {output_path}",
                candidate_id=candidate_id,
                input_path=source_video,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        tolerance = float(merged_config.get("duration_tolerance_sec", 1.0))
        duration_delta = abs(actual_duration - expected_duration)
        if duration_delta > tolerance:
            return _fail(
                "duration_mismatch",
                (
                    f"actual duration {actual_duration:.3f}s differs from expected "
                    f"{expected_duration:.3f}s by {duration_delta:.3f}s "
                    f"(tolerance {tolerance:.3f}s)"
                ),
                candidate_id=candidate_id,
                input_path=source_video,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
                extra_metadata={
                    "expected_duration_sec": round(expected_duration, 3),
                    "actual_duration_sec": round(actual_duration, 3),
                    "duration_delta_sec": round(duration_delta, 3),
                },
            )

        # ------------------------------------------------------------------
        # 9. Return PASS result
        # ------------------------------------------------------------------
        duration_sec_field = selected.get("duration_sec")
        warnings: list[str] = []
        if duration_sec_field is not None and _is_finite_float(duration_sec_field):
            declared_dur = float(duration_sec_field)
            if abs(declared_dur - expected_duration) > 0.5:
                warnings.append(
                    f"candidate duration_sec ({declared_dur:.3f}s) differs "
                    f"from end_sec-start_sec ({expected_duration:.3f}s)"
                )

        return make_module_pass_result(
            MODULE_NAME,
            MODULE_VERSION,
            input_path=source_video,
            output_path=output_path,
            config=merged_config,
            warnings=warnings,
            metadata={
                "candidate_id": candidate_id,
                "start_sec": round(start_sec, 3),
                "end_sec": round(end_sec, 3),
                "expected_duration_sec": round(expected_duration, 3),
                "actual_duration_sec": round(actual_duration, 3),
                "duration_delta_sec": round(abs(actual_duration - expected_duration), 3),
                "ffmpeg_command_summary": cmd_summary,
                "output_file_size_bytes": output_size,
            },
        )


# ---------------------------------------------------------------------------
# Conveyor registry helper
# ---------------------------------------------------------------------------

RENDER_CLIP_V1_MODULE = RenderClipV1Module()


def get_render_clip_v1_module() -> RenderClipV1Module:
    """Return a fresh RenderClipV1Module instance for the conveyor registry."""
    return RenderClipV1Module()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_output_path(clip_dir: str, job_id: str, candidate_id: str, *, ext: str = ".mp4") -> str:
    """Build a deterministic, safe output path for the rendered clip."""
    safe_job = _safe_filename_part(job_id)
    safe_cand = _safe_filename_part(candidate_id)
    filename = f"{safe_job}_{safe_cand}_render_clip_v1{ext}"
    return os.path.join(clip_dir, filename)


def _safe_filename_part(value: str) -> str:
    """Replace non-alphanumeric characters with underscores for safe filenames."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(value))


def _build_ffmpeg_command(
    *,
    source_video: str,
    output_path: str,
    start_sec: float,
    duration_sec: float,
    config: dict[str, Any],
) -> list[str]:
    """Build the ffmpeg subprocess args list.

    Uses ``-ss`` before ``-i`` for fast seek, then ``-t`` for duration.
    Re-encoding by default for timestamp accuracy; stream copy when
    ``copy_streams=True``.
    """
    copy_streams = bool(config.get("copy_streams", False))
    cmd = [
        "ffmpeg",
        "-y",
        "-ss", f"{start_sec:.6f}",
        "-i", source_video,
        "-t", f"{duration_sec:.6f}",
    ]
    if copy_streams:
        cmd += ["-c", "copy"]
    else:
        video_codec = str(config.get("video_codec", "libx264"))
        audio_codec = str(config.get("audio_codec", "aac"))
        preset = str(config.get("ffmpeg_preset", "veryfast"))
        cmd += [
            "-c:v", video_codec,
            "-preset", preset,
            "-c:a", audio_codec,
        ]
    cmd.append(output_path)
    return cmd


def _validate_timestamps(
    start_sec: Any,
    end_sec: Any,
    candidate_id: str,
    config: dict[str, Any],
) -> "_FailResult | None":
    """Return a _FailResult if timestamps are invalid, else None."""
    if start_sec is None:
        return _FailResult("missing_start_sec", "selected_candidate is missing start_sec", candidate_id)

    if end_sec is None:
        return _FailResult("missing_end_sec", "selected_candidate is missing end_sec", candidate_id)

    if not _is_finite_float(start_sec):
        return _FailResult(
            "invalid_timestamp",
            f"start_sec is not a valid number: {start_sec!r}",
            candidate_id,
        )
    if not _is_finite_float(end_sec):
        return _FailResult(
            "invalid_timestamp",
            f"end_sec is not a valid number: {end_sec!r}",
            candidate_id,
        )

    start = float(start_sec)
    end = float(end_sec)

    if start < 0:
        return _FailResult(
            "invalid_timestamp",
            f"start_sec must be >= 0, got {start}",
            candidate_id,
        )
    if end <= start:
        return _FailResult(
            "invalid_timestamp",
            f"end_sec ({end}) must be > start_sec ({start})",
            candidate_id,
        )

    duration = end - start
    min_dur = float(config.get("min_duration_sec", 1.0))
    max_dur = float(config.get("max_duration_sec", 180.0))

    if duration < min_dur:
        return _FailResult(
            "duration_too_short",
            f"calculated duration {duration:.3f}s is below min_duration_sec {min_dur:.3f}s",
            candidate_id,
        )
    if duration > max_dur:
        return _FailResult(
            "duration_too_long",
            f"calculated duration {duration:.3f}s exceeds max_duration_sec {max_dur:.3f}s",
            candidate_id,
        )

    return None


def _validate_render_config(config: dict[str, Any]) -> str | None:
    """Return an error string if config is invalid, else None."""
    min_dur = config.get("min_duration_sec")
    max_dur = config.get("max_duration_sec")
    tolerance = config.get("duration_tolerance_sec")
    output_ext = config.get("output_ext")

    if not _is_finite_float(min_dur) or float(min_dur) < 0:
        return f"invalid min_duration_sec: {min_dur!r}"
    if not _is_finite_float(max_dur) or float(max_dur) <= 0:
        return f"invalid max_duration_sec: {max_dur!r}"
    if float(max_dur) < float(min_dur):
        return f"max_duration_sec ({max_dur}) < min_duration_sec ({min_dur})"
    if not _is_finite_float(tolerance) or float(tolerance) < 0:
        return f"invalid duration_tolerance_sec: {tolerance!r}"
    if not isinstance(output_ext, str) or not output_ext.startswith("."):
        return f"invalid output_ext: {output_ext!r}"
    return None


def _is_finite_float(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


# ---------------------------------------------------------------------------
# Internal failure result helper
# ---------------------------------------------------------------------------


class _FailResult:
    """Lightweight container for a pending FAIL result that needs input_path injected."""

    def __init__(
        self,
        failure_code: str,
        message: str,
        candidate_id: str | None,
        *,
        ffmpeg_returncode: int | None = None,
        ffmpeg_stderr_tail: str | None = None,
        ffmpeg_command_summary: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ):
        self.failure_code = failure_code
        self.message = message
        self.candidate_id = candidate_id
        self.ffmpeg_returncode = ffmpeg_returncode
        self.ffmpeg_stderr_tail = ffmpeg_stderr_tail
        self.ffmpeg_command_summary = ffmpeg_command_summary
        self.extra_metadata = extra_metadata or {}

    def _replace_input(self, input_path: str | None) -> dict[str, Any]:
        return _fail(
            self.failure_code,
            self.message,
            candidate_id=self.candidate_id,
            input_path=input_path,
            ffmpeg_returncode=self.ffmpeg_returncode,
            ffmpeg_stderr_tail=self.ffmpeg_stderr_tail,
            ffmpeg_command_summary=self.ffmpeg_command_summary,
            extra_metadata=self.extra_metadata,
        )


def _fail(
    failure_code: str,
    message: str,
    *,
    candidate_id: str | None,
    input_path: str | None,
    ffmpeg_returncode: int | None = None,
    ffmpeg_stderr_tail: str | None = None,
    ffmpeg_command_summary: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standard FAIL module result."""
    metadata: dict[str, Any] = {
        "candidate_id": candidate_id,
        "failure_code": failure_code,
    }
    if ffmpeg_returncode is not None:
        metadata["ffmpeg_returncode"] = ffmpeg_returncode
    if ffmpeg_stderr_tail is not None:
        metadata["ffmpeg_stderr_tail"] = ffmpeg_stderr_tail
    if ffmpeg_command_summary is not None:
        metadata["ffmpeg_command_summary"] = ffmpeg_command_summary
    if extra_metadata:
        metadata.update(extra_metadata)

    return make_module_fail_result(
        MODULE_NAME,
        MODULE_VERSION,
        message,
        input_path=input_path,
        output_path=None,
        metadata=metadata,
    )
