"""validation_v1 — fourth module in the fixed MK1 universal conveyor.

Validates the final captioned clip produced by intelligent_captions_v1 before
metadata writing.  The module answers only:

    Did this finished clip pass the required MK1 technical checks?

The output is:

    PASS  — all deterministic checks passed.
    FAIL  — one or more checks failed.

This module deliberately does NOT:
- write per-clip metadata files or post_processing_report.json
- register output funnels
- perform recursive quality improvement loops
- call AI/LLM services
- implement metadata_writer_v1
- perform face/object tracking, creative reframing, or audio normalisation
- OCR video output for caption verification
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from typing import Any

from post_processing_modules import (
    PostProcessingModule,
    make_module_fail_result,
    make_module_pass_result,
)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

MODULE_NAME = "validation_v1"
MODULE_VERSION = "1.0"

FFPROBE_TIMEOUT_SEC = 30

REQUIRED_UPSTREAM_MODULES = [
    "render_clip_v1",
    "platform_safe_format_v1",
    "intelligent_captions_v1",
]

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict[str, Any] = {
    "duration_tolerance_sec": 1.25,
    "target_aspect_ratio": 9 / 16,
    "aspect_ratio_tolerance": 0.02,
    "require_upstream_modules": True,
    "required_upstream_modules": list(REQUIRED_UPSTREAM_MODULES),
    "require_audio_if_expected": True,
    "require_caption_metadata_if_captions_ran": True,
}

# ---------------------------------------------------------------------------
# Public module class
# ---------------------------------------------------------------------------


class ValidationV1Module(PostProcessingModule):
    """Final deterministic MK1 validation module.

    Validates the captioned clip from intelligent_captions_v1, checking:
    - Input file existence, non-emptiness
    - ffprobe playability and format duration
    - Video stream presence and valid dimensions
    - Duration within tolerance of selected candidate
    - All required upstream modules passed
    - Upstream output path integrity
    - 9:16 aspect ratio (after platform formatting)
    - Audio presence when expected
    - Caption sidecar/metadata when captions ran

    Plugs directly into :func:`run_module_chain` and
    :func:`run_fixed_mk1_universal_conveyor` as the fourth module.
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
        # Merge config: defaults < direct config < context config
        ctx_config = context.get("config") or {}
        v1_ctx_config = ctx_config.get("validation_v1") or {}
        merged_config = {**_DEFAULT_CONFIG, **(config or {}), **v1_ctx_config}

        # Validate numeric config values early
        config_err = _validate_config(merged_config)
        if config_err:
            return _fail(
                "invalid_validation_config",
                config_err,
                input_path=input_path,
            )

        module_results: list[dict[str, Any]] = list(context.get("module_results") or [])
        selected: dict[str, Any] = dict(context.get("selected_candidate") or {})

        # ------------------------------------------------------------------
        # 1. Input path checks
        # ------------------------------------------------------------------
        if not input_path or not str(input_path).strip():
            return _fail(
                "missing_input_path",
                "input_path is missing or empty",
                input_path=input_path,
            )
        input_path = str(input_path)

        if not os.path.exists(input_path):
            return _fail(
                "input_file_not_found",
                f"input file does not exist: {input_path}",
                input_path=input_path,
            )
        if not os.path.isfile(input_path):
            return _fail(
                "input_path_not_file",
                f"input path is not a regular file: {input_path}",
                input_path=input_path,
            )
        file_size_bytes = os.path.getsize(input_path)
        if file_size_bytes == 0:
            return _fail(
                "input_file_empty",
                f"input file is empty: {input_path}",
                input_path=input_path,
                metadata={"file_size_bytes": 0},
            )

        # ------------------------------------------------------------------
        # 2. ffprobe / playability
        # ------------------------------------------------------------------
        probe_result = _probe_video_info(input_path)
        if probe_result is None:
            return _fail(
                "ffprobe_failed",
                f"ffprobe returned no usable information for: {input_path}",
                input_path=input_path,
                metadata={"file_size_bytes": file_size_bytes},
            )
        if probe_result == "unavailable":
            return _fail(
                "ffprobe_unavailable",
                "ffprobe is not available on this system",
                input_path=input_path,
                metadata={"file_size_bytes": file_size_bytes},
            )

        info: dict[str, Any] = probe_result  # type: ignore[assignment]
        actual_duration: float | None = info["duration_sec"]

        if actual_duration is None:
            return _fail(
                "missing_duration",
                f"no usable format duration found by ffprobe for: {input_path}",
                input_path=input_path,
                metadata={"file_size_bytes": file_size_bytes},
            )
        if actual_duration <= 0:
            return _fail(
                "invalid_duration",
                f"ffprobe reported non-positive duration {actual_duration}s for: {input_path}",
                input_path=input_path,
                metadata={"file_size_bytes": file_size_bytes, "duration_sec": actual_duration},
            )

        # ------------------------------------------------------------------
        # 3. Video stream check
        # ------------------------------------------------------------------
        width: int = info["width"]
        height: int = info["height"]
        video_stream_count: int = info["video_stream_count"]
        audio_stream_count: int = info["audio_stream_count"]

        if video_stream_count == 0 or width <= 0 or height <= 0:
            return _fail(
                "missing_video_stream",
                (
                    f"no valid video stream found "
                    f"(streams={video_stream_count}, w={width}, h={height})"
                ),
                input_path=input_path,
                metadata={
                    "file_size_bytes": file_size_bytes,
                    "duration_sec": actual_duration,
                    "video_stream_count": video_stream_count,
                    "width": width,
                    "height": height,
                },
            )

        if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
            return _fail(
                "invalid_video_dimensions",
                f"video dimensions are not valid positive integers: w={width!r}, h={height!r}",
                input_path=input_path,
                metadata={"file_size_bytes": file_size_bytes},
            )

        # ------------------------------------------------------------------
        # 4. Duration validity (compare against best available expected)
        # ------------------------------------------------------------------
        duration_tolerance = float(merged_config["duration_tolerance_sec"])
        expected_duration: float | None = _resolve_expected_duration(selected, module_results)

        if expected_duration is None:
            # No candidate timestamps at all → fail with missing_selected_candidate
            # only if config requires it; we always require it for MK1.
            return _fail(
                "missing_selected_candidate",
                "could not resolve expected duration from selected_candidate or module metadata",
                input_path=input_path,
                metadata={
                    "file_size_bytes": file_size_bytes,
                    "duration_sec": round(actual_duration, 3),
                },
            )

        if expected_duration <= 0:
            return _fail(
                "invalid_candidate_timestamps",
                f"resolved expected duration is not positive: {expected_duration}s",
                input_path=input_path,
                metadata={
                    "file_size_bytes": file_size_bytes,
                    "duration_sec": round(actual_duration, 3),
                    "expected_duration_sec": round(expected_duration, 3),
                },
            )

        duration_delta = abs(actual_duration - expected_duration)
        if duration_delta > duration_tolerance:
            return _fail(
                "duration_mismatch",
                (
                    f"actual duration {actual_duration:.3f}s differs from expected "
                    f"{expected_duration:.3f}s by {duration_delta:.3f}s "
                    f"(tolerance {duration_tolerance:.3f}s)"
                ),
                input_path=input_path,
                metadata={
                    "file_size_bytes": file_size_bytes,
                    "duration_sec": round(actual_duration, 3),
                    "expected_duration_sec": round(expected_duration, 3),
                    "duration_delta_sec": round(duration_delta, 3),
                    "duration_tolerance_sec": duration_tolerance,
                },
            )

        # ------------------------------------------------------------------
        # 5. Required upstream modules completed
        # ------------------------------------------------------------------
        require_upstream = bool(merged_config.get("require_upstream_modules", True))
        required_names: list[str] = list(
            merged_config.get("required_upstream_modules") or REQUIRED_UPSTREAM_MODULES
        )

        if require_upstream:
            results_by_name = _index_module_results(module_results)
            missing_required: list[str] = []
            failed_required: list[str] = []

            for name in required_names:
                if name not in results_by_name:
                    missing_required.append(name)
                elif results_by_name[name].get("status") != "PASS":
                    failed_required.append(name)

            if missing_required:
                return _fail(
                    "missing_required_module_result",
                    f"required upstream module results are missing: {missing_required}",
                    input_path=input_path,
                    metadata={
                        "file_size_bytes": file_size_bytes,
                        "duration_sec": round(actual_duration, 3),
                        "required_modules_checked": required_names,
                        "missing_required_modules": missing_required,
                        "failed_required_modules": failed_required,
                    },
                )

            if failed_required:
                return _fail(
                    "required_module_failed",
                    f"required upstream modules did not pass: {failed_required}",
                    input_path=input_path,
                    metadata={
                        "file_size_bytes": file_size_bytes,
                        "duration_sec": round(actual_duration, 3),
                        "required_modules_checked": required_names,
                        "missing_required_modules": [],
                        "failed_required_modules": failed_required,
                    },
                )
        else:
            results_by_name = _index_module_results(module_results)

        # ------------------------------------------------------------------
        # 6. Output path integrity
        # ------------------------------------------------------------------
        captions_result = results_by_name.get("intelligent_captions_v1")
        if captions_result is not None:
            captions_output_path = captions_result.get("output_path")
            if not captions_output_path:
                return _fail(
                    "missing_upstream_output_path",
                    "intelligent_captions_v1 result has no output_path",
                    input_path=input_path,
                    metadata={
                        "file_size_bytes": file_size_bytes,
                        "duration_sec": round(actual_duration, 3),
                    },
                )
            # Resolve both paths before comparing to handle relative/trailing-slash diffs
            resolved_input = os.path.realpath(input_path)
            resolved_captions_out = os.path.realpath(str(captions_output_path))
            if resolved_input != resolved_captions_out:
                return _fail(
                    "final_output_path_mismatch",
                    (
                        f"validation input_path does not match intelligent_captions_v1 "
                        f"output_path: {input_path!r} vs {captions_output_path!r}"
                    ),
                    input_path=input_path,
                    metadata={
                        "file_size_bytes": file_size_bytes,
                        "duration_sec": round(actual_duration, 3),
                        "expected_upstream_output_path": str(captions_output_path),
                    },
                )

        # ------------------------------------------------------------------
        # 7. 9:16 aspect ratio (only if platform_safe_format_v1 ran)
        # ------------------------------------------------------------------
        psf_result = results_by_name.get("platform_safe_format_v1")
        aspect_ratio = width / height if height > 0 else 0.0
        target_aspect_ratio = float(merged_config.get("target_aspect_ratio", 9 / 16))
        aspect_ratio_tolerance = float(merged_config.get("aspect_ratio_tolerance", 0.02))
        aspect_ratio_delta = abs(aspect_ratio - target_aspect_ratio)

        if psf_result is not None:
            if aspect_ratio_delta > aspect_ratio_tolerance:
                return _fail(
                    "aspect_ratio_mismatch",
                    (
                        f"aspect ratio {aspect_ratio:.6f} differs from target "
                        f"{target_aspect_ratio:.6f} by {aspect_ratio_delta:.6f} "
                        f"(tolerance {aspect_ratio_tolerance})"
                    ),
                    input_path=input_path,
                    metadata={
                        "file_size_bytes": file_size_bytes,
                        "duration_sec": round(actual_duration, 3),
                        "width": width,
                        "height": height,
                        "aspect_ratio": round(aspect_ratio, 6),
                        "target_aspect_ratio": round(target_aspect_ratio, 6),
                        "aspect_ratio_delta": round(aspect_ratio_delta, 6),
                    },
                )

        # ------------------------------------------------------------------
        # 8. Audio presence if expected
        # ------------------------------------------------------------------
        audio_expected = _resolve_audio_expected(results_by_name)
        require_audio = bool(merged_config.get("require_audio_if_expected", True))

        has_audio = audio_stream_count > 0

        if require_audio and audio_expected and not has_audio:
            return _fail(
                "missing_expected_audio",
                "upstream metadata indicates audio was present but no audio stream found",
                input_path=input_path,
                metadata={
                    "file_size_bytes": file_size_bytes,
                    "duration_sec": round(actual_duration, 3),
                    "audio_stream_count": audio_stream_count,
                    "audio_expected": True,
                },
            )

        # ------------------------------------------------------------------
        # 9. Caption metadata
        # ------------------------------------------------------------------
        caption_metadata_checked = False
        caption_sidecar_path: str | None = None
        require_caption_meta = bool(
            merged_config.get("require_caption_metadata_if_captions_ran", True)
        )

        if captions_result is not None and require_caption_meta:
            caption_metadata_checked = True
            captions_meta = captions_result.get("metadata") or {}

            # Check caption count
            caption_count = captions_meta.get("caption_count")
            if caption_count is not None and isinstance(caption_count, int) and caption_count <= 0:
                return _fail(
                    "missing_caption_metadata",
                    f"intelligent_captions_v1 reports zero captions (caption_count={caption_count})",
                    input_path=input_path,
                    metadata={
                        "file_size_bytes": file_size_bytes,
                        "duration_sec": round(actual_duration, 3),
                        "caption_count": caption_count,
                    },
                )

            # Check sidecar path
            sidecar = captions_meta.get("caption_sidecar_path")
            if sidecar:
                caption_sidecar_path = str(sidecar)
                if not os.path.exists(caption_sidecar_path):
                    return _fail(
                        "caption_sidecar_not_found",
                        f"caption sidecar path was recorded but file does not exist: {caption_sidecar_path}",
                        input_path=input_path,
                        metadata={
                            "file_size_bytes": file_size_bytes,
                            "duration_sec": round(actual_duration, 3),
                            "caption_sidecar_path": caption_sidecar_path,
                        },
                    )
                if not os.path.isfile(caption_sidecar_path):
                    return _fail(
                        "missing_caption_sidecar",
                        f"caption sidecar path is not a regular file: {caption_sidecar_path}",
                        input_path=input_path,
                        metadata={
                            "file_size_bytes": file_size_bytes,
                            "duration_sec": round(actual_duration, 3),
                            "caption_sidecar_path": caption_sidecar_path,
                        },
                    )
                if os.path.getsize(caption_sidecar_path) == 0:
                    return _fail(
                        "caption_sidecar_empty",
                        f"caption sidecar file is empty: {caption_sidecar_path}",
                        input_path=input_path,
                        metadata={
                            "file_size_bytes": file_size_bytes,
                            "duration_sec": round(actual_duration, 3),
                            "caption_sidecar_path": caption_sidecar_path,
                        },
                    )

        # ------------------------------------------------------------------
        # 10. PASS — all checks passed
        # ------------------------------------------------------------------
        return make_module_pass_result(
            MODULE_NAME,
            MODULE_VERSION,
            input_path=input_path,
            output_path=input_path,  # validation does not transform the file
            metadata={
                "validated_output_path": input_path,
                "duration_sec": round(actual_duration, 3),
                "expected_duration_sec": round(expected_duration, 3),
                "duration_delta_sec": round(duration_delta, 3),
                "duration_tolerance_sec": duration_tolerance,
                "width": width,
                "height": height,
                "aspect_ratio": round(aspect_ratio, 6),
                "target_aspect_ratio": round(target_aspect_ratio, 6),
                "aspect_ratio_delta": round(aspect_ratio_delta, 6),
                "video_stream_count": video_stream_count,
                "audio_stream_count": audio_stream_count,
                "audio_expected": audio_expected,
                "required_modules_checked": list(required_names) if require_upstream else [],
                "caption_metadata_checked": caption_metadata_checked,
                "caption_sidecar_path": caption_sidecar_path,
                "file_size_bytes": file_size_bytes,
            },
        )


# ---------------------------------------------------------------------------
# Conveyor registry helpers
# ---------------------------------------------------------------------------

VALIDATION_V1_MODULE = ValidationV1Module()


def get_validation_v1_module() -> ValidationV1Module:
    """Return a fresh ValidationV1Module instance for the conveyor registry."""
    return ValidationV1Module()


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------

_VideoInfo = dict[str, Any]  # width, height, duration_sec, audio_stream_count, video_stream_count


def _probe_video_info(path: str) -> _VideoInfo | None | str:
    """Probe a video file and return a video info dict.

    Returns:
        ``"unavailable"`` if ffprobe is not installed.
        ``None`` if the probe fails or yields no usable data.
        Dict with ``{width, height, duration_sec, has_audio, video_stream_count, audio_stream_count}``
        on success.
    """
    if not shutil.which("ffprobe"):
        return "unavailable"

    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-hide_banner",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SEC,
        )
    except Exception:
        return None

    if proc.returncode != 0:
        return None

    try:
        data: dict[str, Any] = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    streams = data.get("streams") or []
    width = 0
    height = 0
    video_stream_count = 0
    audio_stream_count = 0

    for s in streams:
        if not isinstance(s, dict):
            continue
        codec_type = str(s.get("codec_type") or "")
        if codec_type == "video":
            video_stream_count += 1
            if video_stream_count == 1:
                try:
                    width = int(s.get("width") or 0)
                    height = int(s.get("height") or 0)
                except (TypeError, ValueError):
                    pass
        elif codec_type == "audio":
            audio_stream_count += 1

    duration_sec: float | None = None
    fmt = data.get("format")
    if isinstance(fmt, dict) and fmt.get("duration") is not None:
        try:
            d = float(fmt["duration"])
            if math.isfinite(d) and d > 0:
                duration_sec = d
        except (TypeError, ValueError):
            pass

    return {
        "width": width,
        "height": height,
        "duration_sec": duration_sec,
        "has_audio": audio_stream_count > 0,
        "video_stream_count": video_stream_count,
        "audio_stream_count": audio_stream_count,
    }


# ---------------------------------------------------------------------------
# Duration resolution
# ---------------------------------------------------------------------------


def _resolve_expected_duration(
    selected: dict[str, Any],
    module_results: list[dict[str, Any]],
) -> float | None:
    """Resolve the best available expected duration for the final clip.

    Priority:
      1. selected_candidate.duration_sec (if valid and positive)
      2. selected_candidate.end_sec - selected_candidate.start_sec
      3. render_clip_v1 metadata: actual_duration_sec or expected_duration_sec
      4. platform_safe_format_v1 metadata: output_duration_sec
      5. intelligent_captions_v1 metadata: output_duration_sec
    """
    # 1. selected_candidate.duration_sec
    dur = selected.get("duration_sec")
    if _is_positive_finite(dur):
        return float(dur)

    # 2. end_sec - start_sec
    start = selected.get("start_sec")
    end = selected.get("end_sec")
    if _is_finite_float(start) and _is_finite_float(end):
        diff = float(end) - float(start)
        if diff > 0:
            return diff

    # 3-5. Prior module metadata
    results_by_name = _index_module_results(module_results)

    for module_name, meta_key in [
        ("render_clip_v1", "actual_duration_sec"),
        ("render_clip_v1", "expected_duration_sec"),
        ("platform_safe_format_v1", "output_duration_sec"),
        ("intelligent_captions_v1", "output_duration_sec"),
    ]:
        result = results_by_name.get(module_name)
        if result is None:
            continue
        meta = result.get("metadata") or {}
        val = meta.get(meta_key)
        if _is_positive_finite(val):
            return float(val)

    return None


# ---------------------------------------------------------------------------
# Audio resolution
# ---------------------------------------------------------------------------


def _resolve_audio_expected(results_by_name: dict[str, dict[str, Any]]) -> bool:
    """Determine whether audio was expected based on upstream module metadata.

    Returns True if any upstream module metadata positively indicates
    audio was present or preserved.
    """
    # Check platform_safe_format_v1 first (most reliable audio indicator)
    for module_name in ("platform_safe_format_v1", "render_clip_v1", "intelligent_captions_v1"):
        result = results_by_name.get(module_name)
        if result is None:
            continue
        meta = result.get("metadata") or {}
        # Look for explicit audio indicators in metadata
        for key in ("input_has_audio", "output_has_audio", "has_audio"):
            val = meta.get(key)
            if isinstance(val, bool):
                if val:
                    return True
                # If explicitly False, keep looking (another module might confirm)
    return False


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def _validate_config(config: dict[str, Any]) -> str | None:
    """Return an error string if config is invalid, else None."""
    tolerance = config.get("duration_tolerance_sec")
    if not _is_finite_float(tolerance) or float(tolerance) < 0:
        return f"duration_tolerance_sec must be a non-negative number, got {tolerance!r}"

    tar = config.get("target_aspect_ratio")
    if not _is_finite_float(tar) or float(tar) <= 0:
        return f"target_aspect_ratio must be a positive number, got {tar!r}"

    art = config.get("aspect_ratio_tolerance")
    if not _is_finite_float(art) or float(art) < 0:
        return f"aspect_ratio_tolerance must be a non-negative number, got {art!r}"

    return None


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------


def _index_module_results(
    module_results: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return a dict mapping module_name → last result for that module."""
    index: dict[str, dict[str, Any]] = {}
    for r in module_results:
        if not isinstance(r, dict):
            continue
        name = r.get("module_name")
        if isinstance(name, str) and name:
            index[name] = r
    return index


# ---------------------------------------------------------------------------
# Float helpers
# ---------------------------------------------------------------------------


def _is_finite_float(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _is_positive_finite(value: Any) -> bool:
    return _is_finite_float(value) and float(value) > 0


# ---------------------------------------------------------------------------
# Failure result helper
# ---------------------------------------------------------------------------


def _fail(
    failure_code: str,
    message: str,
    *,
    input_path: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standard FAIL module result for this module.

    The failure_code is used as error_reason so it is machine-readable.
    The descriptive message is stored in metadata.
    """
    base_metadata: dict[str, Any] = {"failure_code": failure_code, "message": message}
    if metadata:
        base_metadata.update(metadata)

    return make_module_fail_result(
        MODULE_NAME,
        MODULE_VERSION,
        failure_code,
        input_path=input_path,
        output_path=None,
        metadata=base_metadata,
    )
