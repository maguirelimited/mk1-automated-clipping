"""platform_safe_format_v1 — second module in the fixed MK1 universal conveyor.

Takes the initially rendered clip from render_clip_v1 and outputs a
deterministic 9:16 vertical MP4 suitable for Shorts/Reels/TikTok distribution.

MK1 formatting strategy: "blurred background + centred foreground"

    1. Create a 1080×1920 background by scaling the input to fill the canvas
       and applying a boxblur so it is visually recessed.
    2. Scale the original clip to fit inside 1080×1920 without stretching
       (letterbox/pillarbox into the vertical canvas).
    3. Overlay the fitted foreground centred on the background.
    4. Preserve audio.

This approach avoids cropping important foreground content while producing
the platform-safe 9:16 canvas every time.

This module deliberately does NOT:
- generate captions or burn subtitles
- perform face / object tracking or intelligent zoom
- perform creative reframing
- normalise audio, trim silence, or optimise endings
- implement final validation (that is validation_v1's responsibility)
- write per-clip metadata files or post_processing_report.json
- call AI/LLM services
- register output funnels
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from typing import Any

from post_processing_modules import (
    PostProcessingModule,
    make_module_fail_result,
    make_module_pass_result,
)
from reframing.config import (
    DEFAULT_REFRAME_MODE,
    FAIL_REASON_FACE_TRACK_NOT_ELIGIBLE,
    FAIL_REASON_FACE_TRACK_PIPELINE_FAILED,
    FAIL_REASON_FACE_TRACK_RENDER_FAILED,
    FORMAT_STRATEGY_BLURRED_BACKGROUND,
    FORMAT_STRATEGY_FACE_TRACK_CROP,
    REFRAME_MODE_FACE_TRACK,
    REFRAME_MODES,
    resolve_reframe_plan,
    validate_reframe_mode,
)
from reframing.fallback import build_blur_background_command
from reframing.eligibility import (
    evaluate_face_track_eligibility,
    face_track_eligibility_metadata,
    write_eligibility_report,
)
from reframing.tracker import face_pipeline_metadata
from reframing.crop_path_planner import crop_path_metadata
from reframing.renderer import face_track_render_metadata, render_face_track_crop
from reframing.smoother import (
    build_smoothed_face_crop_path_for_clip,
    smoothed_crop_path_metadata,
)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

MODULE_NAME = "platform_safe_format_v1"
MODULE_VERSION = "1.0"

# Backward-compatible aliases for tests and downstream metadata consumers.
FORMAT_STRATEGY_BLUR = FORMAT_STRATEGY_BLURRED_BACKGROUND
FORMAT_STRATEGY_FACE_TRACK = FORMAT_STRATEGY_FACE_TRACK_CROP
FORMAT_STRATEGY = FORMAT_STRATEGY_BLURRED_BACKGROUND

FFMPEG_TIMEOUT_SEC = 180
FFPROBE_TIMEOUT_SEC = 30

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict[str, Any] = {
    "target_width": 1080,
    "target_height": 1920,
    "output_ext": ".mp4",
    "duration_tolerance_sec": 1.0,
    "ffmpeg_preset": "veryfast",
    "video_codec": "libx264",
    "audio_codec": "aac",
    "background_mode": "blurred",
    "background_blur": "20:1",
    "safe_zone_top_px": 180,
    "safe_zone_bottom_px": 320,
    "safe_zone_left_px": 80,
    "safe_zone_right_px": 80,
    "overwrite": True,
    "reframe_mode": DEFAULT_REFRAME_MODE,
    "face_track_test_enabled": False,
}

# ---------------------------------------------------------------------------
# Public module class
# ---------------------------------------------------------------------------


class PlatformSafeFormatV1Module(PostProcessingModule):
    """Real MK1 platform-safe format module.

    Converts any input clip to a 9:16 vertical MP4 using the deterministic
    MK1 "blurred background + centred foreground" strategy.

    Plugs directly into :func:`run_module_chain` and
    :func:`run_fixed_mk1_universal_conveyor` as the second module.
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

        candidate_id: str | None = None
        try:
            cand = context.get("selected_candidate") or {}
            if isinstance(cand, dict):
                candidate_id = cand.get("candidate_id") or None
        except Exception:
            pass

        # ------------------------------------------------------------------
        # 1. Validate config before touching any files
        # ------------------------------------------------------------------
        config_error = _validate_format_config(merged_config)
        if config_error:
            return _fail(
                "invalid_format_config",
                config_error,
                candidate_id=candidate_id,
                input_path=input_path,
            )

        target_w: int = int(merged_config["target_width"])
        target_h: int = int(merged_config["target_height"])

        reframe_plan = resolve_reframe_plan(merged_config)
        if reframe_plan.get("fail_reason"):
            return _fail(
                "reframe_not_implemented",
                reframe_plan["fail_reason"],
                candidate_id=candidate_id,
                input_path=input_path,
            )

        face_pipeline_meta: dict[str, Any] = {}
        face_render_meta: dict[str, Any] = {}
        pipeline_warnings: list[str] = list(reframe_plan.get("warnings") or [])
        face_track_test_enabled = bool(reframe_plan.get("face_track_test_enabled", False))
        face_track_skip_reason = reframe_plan.get("face_track_skip_reason")
        if face_track_skip_reason == "face_track_test_disabled":
            pipeline_warnings.append("face_track_test_disabled_using_blur_fallback")
        use_face_track_render = False
        smoothed_crop_report = None
        eligibility_report = None

        # ------------------------------------------------------------------
        # 2. Validate input file
        # ------------------------------------------------------------------
        if not input_path or not str(input_path).strip():
            return _fail(
                "missing_input_path",
                "input_path is missing or empty",
                candidate_id=candidate_id,
                input_path=input_path,
            )
        input_path = str(input_path)

        if not os.path.exists(input_path):
            return _fail(
                "input_file_not_found",
                f"input file does not exist: {input_path}",
                candidate_id=candidate_id,
                input_path=input_path,
            )
        if not os.path.isfile(input_path):
            return _fail(
                "input_path_not_file",
                f"input path is not a regular file: {input_path}",
                candidate_id=candidate_id,
                input_path=input_path,
            )
        if os.path.getsize(input_path) == 0:
            return _fail(
                "input_file_empty",
                f"input file is empty: {input_path}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        # ------------------------------------------------------------------
        # 3. Probe input
        # ------------------------------------------------------------------
        try:
            input_info = _probe_video_info(input_path)
        except Exception as exc:
            return _fail(
                "input_probe_failed",
                f"could not probe input file: {exc}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        if input_info is None:
            return _fail(
                "input_probe_failed",
                f"ffprobe returned no usable information for: {input_path}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        if input_info["width"] <= 0 or input_info["height"] <= 0:
            return _fail(
                "missing_video_stream",
                f"input has no valid video stream (probed w={input_info['width']} h={input_info['height']})",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        input_w = input_info["width"]
        input_h = input_info["height"]
        input_duration = input_info["duration_sec"]
        input_has_audio = input_info["has_audio"]

        # ------------------------------------------------------------------
        # 4. Resolve output path
        # ------------------------------------------------------------------
        job_id: str = str(context.get("job_id") or "job_unknown")
        clip_dir: str | None = context.get("clip_dir")
        if not clip_dir:
            clip_dir = os.path.join(os.path.dirname(input_path), "formatted")

        output_path = _make_output_path(
            clip_dir,
            job_id,
            candidate_id or "unknown",
            ext=str(merged_config.get("output_ext", ".mp4")),
        )

        overwrite = bool(merged_config.get("overwrite", True))
        if os.path.exists(output_path) and not overwrite:
            return _fail(
                "output_exists",
                f"output file already exists and overwrite=false: {output_path}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        try:
            os.makedirs(clip_dir, exist_ok=True)
        except OSError as exc:
            return _fail(
                "unexpected_format_error",
                f"could not create clip directory: {exc}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        # ------------------------------------------------------------------
        # 4b. Optional face pipeline: detect → track → crop plan → smooth
        # ------------------------------------------------------------------
        if reframe_plan.get("attempt_face_pipeline"):
            tmp_dir = str(context.get("tmp_dir") or clip_dir)
            detection_report, track_report, crop_report, smoothed_crop_report = (
                build_smoothed_face_crop_path_for_clip(
                    input_path,
                    source_width=input_w,
                    source_height=input_h,
                    tmp_dir=tmp_dir,
                    config=merged_config,
                )
            )
            face_pipeline_meta = {
                **face_pipeline_metadata(
                    detection_report=detection_report,
                    track_report=track_report,
                    reframe_attempted=True,
                ),
                **crop_path_metadata(crop_report=crop_report),
                **smoothed_crop_path_metadata(smoothed_report=smoothed_crop_report),
            }
            reframe_plan["reframe_attempted"] = True

            eligibility_report = evaluate_face_track_eligibility(
                detection_report,
                track_report,
                crop_path_report=crop_report,
                smoothed_crop_path_report=smoothed_crop_report,
                clip_duration_sec=input_duration,
                config=merged_config,
            )
            face_pipeline_meta.update(
                face_track_eligibility_metadata(
                    eligibility_report,
                    reframe_mode=reframe_plan["reframe_mode"],
                    use_blur_fallback=bool(reframe_plan.get("use_blur_fallback")),
                )
            )
            eligibility_report_path = context.get("eligibility_report_path") or os.path.join(
                clip_dir,
                "face_track_eligibility_report.json",
            )
            write_eligibility_report(str(eligibility_report_path), eligibility_report)

            if not detection_report.ok:
                pipeline_warnings.append("face_detection_failed_using_blur_fallback")
            elif not track_report.ok:
                pipeline_warnings.append("face_tracking_failed_using_blur_fallback")
            elif not eligibility_report.eligible:
                if reframe_plan.get("use_blur_fallback"):
                    pipeline_warnings.append("face_track_not_eligible_using_blur_fallback")
                elif not track_report.usable:
                    pipeline_warnings.append("face_track_not_usable_using_blur_fallback")
            elif not track_report.usable:
                pipeline_warnings.append("face_track_not_usable_using_blur_fallback")
            elif not crop_report.ok or not crop_report.usable:
                pipeline_warnings.append("crop_path_not_usable_using_blur_fallback")
            elif not smoothed_crop_report.ok or not smoothed_crop_report.usable:
                pipeline_warnings.append("smoothed_crop_path_not_usable_using_blur_fallback")

            pipeline_usable = _face_pipeline_usable(
                detection_report=detection_report,
                track_report=track_report,
                crop_report=crop_report,
                smoothed_crop_report=smoothed_crop_report,
            )

            if (
                reframe_plan["reframe_mode"] == REFRAME_MODE_FACE_TRACK
                and not eligibility_report.eligible
            ):
                return _fail(
                    "face_track_not_eligible",
                    eligibility_report.message or FAIL_REASON_FACE_TRACK_NOT_ELIGIBLE,
                    candidate_id=candidate_id,
                    input_path=input_path,
                    extra_metadata=face_pipeline_meta,
                )

            if (
                reframe_plan["reframe_mode"] == REFRAME_MODE_FACE_TRACK
                and not pipeline_usable
            ):
                return _fail(
                    "face_track_pipeline_failed",
                    FAIL_REASON_FACE_TRACK_PIPELINE_FAILED,
                    candidate_id=candidate_id,
                    input_path=input_path,
                    extra_metadata=face_pipeline_meta,
                )

            if eligibility_report.eligible and pipeline_usable:
                use_face_track_render = True
                reframe_plan["format_strategy"] = FORMAT_STRATEGY_FACE_TRACK_CROP

        # ------------------------------------------------------------------
        # 5. Build and run ffmpeg / face-track renderer
        # ------------------------------------------------------------------
        cmd_summary = ""
        render_result = None

        if use_face_track_render and smoothed_crop_report is not None:
            render_result = render_face_track_crop(
                input_path=input_path,
                output_path=output_path,
                smoothed_crop_report=smoothed_crop_report,
                config=merged_config,
                tmp_dir=str(context.get("tmp_dir") or clip_dir),
                input_has_audio=input_has_audio,
                clip_duration_sec=input_duration,
                render_id=f"{job_id}_{candidate_id or 'unknown'}",
            )
            face_render_meta = face_track_render_metadata(render_result=render_result)
            cmd_summary = render_result.ffmpeg_command_summary or ""

            if not render_result.ok:
                if reframe_plan.get("use_blur_fallback"):
                    pipeline_warnings.append("face_track_render_failed_using_blur_fallback")
                    use_face_track_render = False
                    reframe_plan["format_strategy"] = FORMAT_STRATEGY_BLURRED_BACKGROUND
                else:
                    return _fail(
                        "face_track_render_failed",
                        render_result.message or FAIL_REASON_FACE_TRACK_RENDER_FAILED,
                        candidate_id=candidate_id,
                        input_path=input_path,
                        extra_metadata={**face_pipeline_meta, **face_render_meta},
                        ffmpeg_command_summary=cmd_summary or None,
                    )

        if not use_face_track_render:
            ffmpeg_cmd = _build_format_command(
                plan=reframe_plan,
                input_path=input_path,
                output_path=output_path,
                target_w=target_w,
                target_h=target_h,
                config=merged_config,
                input_has_audio=input_has_audio,
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
                    input_path=input_path,
                    ffmpeg_returncode=None,
                    ffmpeg_stderr_tail="timeout",
                    ffmpeg_command_summary=cmd_summary,
                )
            except Exception as exc:
                return _fail(
                    "unexpected_format_error",
                    f"unexpected error launching ffmpeg: {exc}",
                    candidate_id=candidate_id,
                    input_path=input_path,
                    ffmpeg_command_summary=cmd_summary,
                )

            if proc.returncode != 0:
                stderr_tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-800:]
                return _fail(
                    "ffmpeg_failed",
                    f"ffmpeg exited with code {proc.returncode}",
                    candidate_id=candidate_id,
                    input_path=input_path,
                    ffmpeg_returncode=proc.returncode,
                    ffmpeg_stderr_tail=stderr_tail or "(no output)",
                    ffmpeg_command_summary=cmd_summary,
                )

        # ------------------------------------------------------------------
        # 7. Verify output
        # ------------------------------------------------------------------
        if not os.path.isfile(output_path):
            return _fail(
                "output_missing",
                f"ffmpeg succeeded but output is absent: {output_path}",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )
        output_size = os.path.getsize(output_path)
        if output_size == 0:
            return _fail(
                "output_empty",
                f"output file exists but is empty: {output_path}",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        try:
            out_info = _probe_video_info(output_path)
        except Exception as exc:
            return _fail(
                "output_probe_failed",
                f"could not probe output file: {exc}",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        if out_info is None or out_info["width"] <= 0:
            return _fail(
                "output_missing_video_stream",
                f"output has no valid video stream: {output_path}",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        # Dimension check
        if out_info["width"] != target_w or out_info["height"] != target_h:
            return _fail(
                "invalid_output_dimensions",
                (
                    f"output dimensions {out_info['width']}x{out_info['height']} "
                    f"do not match target {target_w}x{target_h}"
                ),
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        # Aspect ratio check (9:16)
        gcd = math.gcd(out_info["width"], out_info["height"])
        ar_w = out_info["width"] // gcd
        ar_h = out_info["height"] // gcd
        if ar_w != 9 or ar_h != 16:
            return _fail(
                "invalid_output_aspect_ratio",
                f"output aspect ratio {ar_w}:{ar_h} is not 9:16",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        # Audio check
        if input_has_audio and not out_info["has_audio"]:
            return _fail(
                "output_missing_audio",
                "input had audio but output has no audio stream",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        # Duration check (only if input duration was reliably probed)
        duration_delta: float = 0.0
        if input_duration is not None and out_info["duration_sec"] is not None:
            duration_delta = abs(out_info["duration_sec"] - input_duration)
            tolerance = float(merged_config.get("duration_tolerance_sec", 1.0))
            if duration_delta > tolerance:
                return _fail(
                    "duration_mismatch",
                    (
                        f"output duration {out_info['duration_sec']:.3f}s differs from "
                        f"input {input_duration:.3f}s by {duration_delta:.3f}s "
                        f"(tolerance {tolerance:.3f}s)"
                    ),
                    candidate_id=candidate_id,
                    input_path=input_path,
                    ffmpeg_returncode=0,
                    ffmpeg_command_summary=cmd_summary,
                )

        # ------------------------------------------------------------------
        # 8. Compute safe-zone metadata
        # ------------------------------------------------------------------
        safe_zones = _compute_safe_zones(merged_config, target_w=target_w, target_h=target_h)

        # ------------------------------------------------------------------
        # 9. Return PASS result
        # ------------------------------------------------------------------
        return make_module_pass_result(
            MODULE_NAME,
            MODULE_VERSION,
            input_path=input_path,
            output_path=output_path,
            config=merged_config,
            warnings=list(pipeline_warnings),
            metadata={
                "candidate_id": candidate_id,
                "input_width": input_w,
                "input_height": input_h,
                "input_duration_sec": round(input_duration, 3) if input_duration is not None else None,
                "input_has_audio": input_has_audio,
                "target_width": target_w,
                "target_height": target_h,
                "output_width": out_info["width"],
                "output_height": out_info["height"],
                "output_duration_sec": round(out_info["duration_sec"], 3) if out_info["duration_sec"] is not None else None,
                "duration_delta_sec": round(duration_delta, 3),
                "aspect_ratio": "9:16",
                "format_strategy": reframe_plan["format_strategy"],
                "reframe_mode": reframe_plan["reframe_mode"],
                "reframe_attempted": reframe_plan["reframe_attempted"],
                "face_track_test_enabled": face_track_test_enabled,
                "face_track_attempted": reframe_plan["reframe_attempted"],
                "face_track_used": use_face_track_render,
                "face_track_skip_reason": face_track_skip_reason,
                **face_pipeline_meta,
                **face_render_meta,
                "safe_zones": safe_zones,
                "ffmpeg_command_summary": cmd_summary,
                "output_file_size_bytes": output_size,
            },
        )


# ---------------------------------------------------------------------------
# Conveyor registry helpers
# ---------------------------------------------------------------------------

PLATFORM_SAFE_FORMAT_V1_MODULE = PlatformSafeFormatV1Module()


def get_platform_safe_format_v1_module() -> PlatformSafeFormatV1Module:
    """Return a fresh PlatformSafeFormatV1Module instance for the conveyor registry."""
    return PlatformSafeFormatV1Module()


# ---------------------------------------------------------------------------
# Face pipeline helpers
# ---------------------------------------------------------------------------


def _face_pipeline_usable(
    *,
    detection_report: Any,
    track_report: Any,
    crop_report: Any,
    smoothed_crop_report: Any,
) -> bool:
    return bool(
        detection_report.ok
        and track_report.ok
        and track_report.usable
        and crop_report.ok
        and crop_report.usable
        and smoothed_crop_report.ok
        and smoothed_crop_report.usable
    )


# ---------------------------------------------------------------------------
# Format command dispatch
# ---------------------------------------------------------------------------


def _build_format_command(
    *,
    plan: dict[str, Any] | None = None,
    input_path: str,
    output_path: str,
    target_w: int,
    target_h: int,
    config: dict[str, Any],
    input_has_audio: bool,
) -> list[str]:
    """Build the ffmpeg command for the active reframe strategy."""
    effective_plan = plan or resolve_reframe_plan(config)
    strategy = effective_plan["format_strategy"]

    if strategy == FORMAT_STRATEGY_BLURRED_BACKGROUND:
        return build_blur_background_command(
            input_path=input_path,
            output_path=output_path,
            target_w=target_w,
            target_h=target_h,
            config=config,
            input_has_audio=input_has_audio,
        )

    raise ValueError(f"unsupported format strategy for ffmpeg render: {strategy!r}")


# Backward-compatible alias used by tests and internal callers.
_resolve_reframe_plan = resolve_reframe_plan


# ---------------------------------------------------------------------------
# Safe-zone computation
# ---------------------------------------------------------------------------


def _compute_safe_zones(config: dict[str, Any], *, target_w: int, target_h: int) -> dict[str, int]:
    top = int(config.get("safe_zone_top_px", 180))
    bottom = int(config.get("safe_zone_bottom_px", 320))
    left = int(config.get("safe_zone_left_px", 80))
    right = int(config.get("safe_zone_right_px", 80))
    return {
        "top_margin_px": top,
        "bottom_margin_px": bottom,
        "left_margin_px": left,
        "right_margin_px": right,
        "caption_safe_y_min_px": top * 2,
        "caption_safe_y_max_px": target_h - bottom * 2,
    }


# ---------------------------------------------------------------------------
# Output path helper
# ---------------------------------------------------------------------------


def _make_output_path(clip_dir: str, job_id: str, candidate_id: str, *, ext: str = ".mp4") -> str:
    safe_job = _safe_filename_part(job_id)
    safe_cand = _safe_filename_part(candidate_id)
    filename = f"{safe_job}_{safe_cand}_platform_safe_format_v1{ext}"
    return os.path.join(clip_dir, filename)


def _safe_filename_part(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(value))


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------

_VideoInfo = dict[str, Any]  # width, height, duration_sec, has_audio


def _probe_video_info(path: str) -> _VideoInfo | None:
    """Probe video file and return a dict with width, height, duration_sec, has_audio.

    Returns None if the probe completely fails.  On partial results (e.g.
    missing duration) the dict is still returned with None for that field.
    """
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
    has_audio = False

    for s in streams:
        if not isinstance(s, dict):
            continue
        codec_type = str(s.get("codec_type") or "")
        if codec_type == "video" and width == 0:
            try:
                width = int(s.get("width") or 0)
                height = int(s.get("height") or 0)
            except (TypeError, ValueError):
                pass
        elif codec_type == "audio":
            has_audio = True

    # Duration from format block
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
        "has_audio": has_audio,
    }


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def _validate_format_config(config: dict[str, Any]) -> str | None:
    """Return an error string if config is invalid, else None."""
    tw = config.get("target_width")
    th = config.get("target_height")

    if not isinstance(tw, int) or isinstance(tw, bool) or tw <= 0:
        return f"invalid target_width: {tw!r}"
    if not isinstance(th, int) or isinstance(th, bool) or th <= 0:
        return f"invalid target_height: {th!r}"

    # Must be 9:16
    gcd = math.gcd(tw, th)
    if tw // gcd != 9 or th // gcd != 16:
        return f"target dimensions {tw}x{th} are not 9:16 (got {tw // gcd}:{th // gcd})"

    tolerance = config.get("duration_tolerance_sec")
    if not _is_finite_float(tolerance) or float(tolerance) < 0:
        return f"invalid duration_tolerance_sec: {tolerance!r}"

    output_ext = config.get("output_ext")
    if not isinstance(output_ext, str) or not output_ext.startswith("."):
        return f"invalid output_ext: {output_ext!r}"

    reframe_mode = config.get("reframe_mode", DEFAULT_REFRAME_MODE)
    reframe_mode_error = validate_reframe_mode(reframe_mode)
    if reframe_mode_error:
        return reframe_mode_error

    return None


def _is_finite_float(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


# ---------------------------------------------------------------------------
# Failure result builder
# ---------------------------------------------------------------------------


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
    metadata: dict[str, Any] = {
        "candidate_id": candidate_id,
        "failure_code": failure_code,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    if ffmpeg_returncode is not None:
        metadata["ffmpeg_returncode"] = ffmpeg_returncode
    if ffmpeg_stderr_tail is not None:
        metadata["ffmpeg_stderr_tail"] = ffmpeg_stderr_tail
    if ffmpeg_command_summary is not None:
        metadata["ffmpeg_command_summary"] = ffmpeg_command_summary

    return make_module_fail_result(
        MODULE_NAME,
        MODULE_VERSION,
        message,
        input_path=input_path,
        output_path=None,
        metadata=metadata,
    )
