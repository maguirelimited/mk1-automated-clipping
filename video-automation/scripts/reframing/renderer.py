"""Segmented FFmpeg face-track crop renderer for MK1 platform-safe formatting."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Callable

from reframing.crop_path_planner import ASPECT_TOLERANCE
from reframing.types import (
    CropPathReport,
    CropPathSample,
    CropRect,
    FaceTrackRenderResult,
    RenderSegment,
    RenderSegmentPlanStats,
)

# ---------------------------------------------------------------------------
# Defaults (internal — not exposed in Ops UI yet)
# ---------------------------------------------------------------------------

DEFAULT_CROP_RENDERER = "segmented_ffmpeg"
DEFAULT_MIN_SEGMENT_DURATION_SEC = 0.25
DEFAULT_SEGMENT_CROP_CHANGE_THRESHOLD_PX = 4
DEFAULT_DURATION_TOLERANCE_SEC = 0.25
DEFAULT_CLEANUP_TEMP_SEGMENTS = True

FFMPEG_TIMEOUT_SEC = 180

REASON_INPUT_NOT_FOUND = "input_not_found"
REASON_CROP_REPORT_NOT_USABLE = "crop_report_not_usable"
REASON_NO_CROP_SAMPLES = "no_crop_samples"
REASON_INVALID_SOURCE_DIMENSIONS = "invalid_source_dimensions"
REASON_INVALID_TARGET_DIMENSIONS = "invalid_target_dimensions"
REASON_INVALID_CROP_RECT = "invalid_crop_rect"
REASON_INVALID_CLIP_DURATION = "invalid_clip_duration"
REASON_SEGMENT_RENDER_FAILED = "segment_render_failed"
REASON_CONCAT_FAILED = "concat_failed"
REASON_OUTPUT_MISSING = "output_missing"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def merge_renderer_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge caller overrides with renderer defaults."""
    merged = {
        "crop_renderer": DEFAULT_CROP_RENDERER,
        "min_segment_duration_sec": DEFAULT_MIN_SEGMENT_DURATION_SEC,
        "segment_crop_change_threshold_px": DEFAULT_SEGMENT_CROP_CHANGE_THRESHOLD_PX,
        "duration_tolerance_sec": DEFAULT_DURATION_TOLERANCE_SEC,
        "cleanup_temp_segments": DEFAULT_CLEANUP_TEMP_SEGMENTS,
        "target_width": 1080,
        "target_height": 1920,
        "video_codec": "libx264",
        "audio_codec": "aac",
        "ffmpeg_preset": "veryfast",
    }
    if config:
        for key in merged:
            if key in config:
                merged[key] = config[key]
    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_face_track_render_plan(
    smoothed_crop_report: CropPathReport,
    *,
    clip_duration_sec: float | None,
    config: dict[str, Any] | None = None,
) -> tuple[list[RenderSegment], RenderSegmentPlanStats | None, str | None]:
    """Validate a smoothed crop path and build optimized segmented render intervals."""
    merged = merge_renderer_config(config)
    error = validate_smoothed_crop_for_render(
        smoothed_crop_report,
        target_width=int(merged["target_width"]),
        target_height=int(merged["target_height"]),
    )
    if error:
        return [], None, error

    duration = _resolve_clip_duration(smoothed_crop_report.samples, clip_duration_sec)
    if duration is None or duration <= 0:
        return [], None, REASON_INVALID_CLIP_DURATION

    segments, plan_stats = plan_render_segments_from_crop_path(
        smoothed_crop_report.samples,
        clip_duration_sec=duration,
        min_segment_duration_sec=float(merged["min_segment_duration_sec"]),
        segment_crop_change_threshold_px=int(merged["segment_crop_change_threshold_px"]),
    )
    if not segments:
        return [], None, REASON_NO_CROP_SAMPLES

    return segments, plan_stats, None


def render_face_track_crop(
    *,
    input_path: str,
    output_path: str,
    smoothed_crop_report: CropPathReport,
    config: dict[str, Any] | None = None,
    tmp_dir: str,
    input_has_audio: bool,
    clip_duration_sec: float | None = None,
    render_id: str = "face_track",
    run_ffmpeg: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> FaceTrackRenderResult:
    """Render a smoothed crop path into a target-size MP4 using segmented FFmpeg."""
    merged = merge_renderer_config(config)
    target_w = int(merged["target_width"])
    target_h = int(merged["target_height"])

    if not input_path or not os.path.isfile(input_path):
        return _failure_result(
            reason=REASON_INPUT_NOT_FOUND,
            message=f"input file does not exist: {input_path}",
            target_width=target_w,
            target_height=target_h,
        )

    segments, plan_stats, plan_error = build_face_track_render_plan(
        smoothed_crop_report,
        clip_duration_sec=clip_duration_sec,
        config=merged,
    )
    if plan_error:
        return _failure_result(
            reason=plan_error,
            message=f"cannot build face-track render plan: {plan_error}",
            target_width=target_w,
            target_height=target_h,
            segment_crop_change_threshold_px=int(
                merged.get("segment_crop_change_threshold_px", DEFAULT_SEGMENT_CROP_CHANGE_THRESHOLD_PX)
            ),
        )

    runner = run_ffmpeg or _default_run_ffmpeg
    segment_dir = os.path.join(tmp_dir, "face_track_render", _safe_render_id(render_id))
    os.makedirs(segment_dir, exist_ok=True)

    segment_paths: list[str] = []
    command_summaries: list[str] = []

    try:
        for index, segment in enumerate(segments):
            segment_path = os.path.join(segment_dir, f"segment_{index:04d}.mp4")
            segment_cmd = build_segment_command(
                input_path=input_path,
                output_path=segment_path,
                segment=segment,
                target_w=target_w,
                target_h=target_h,
                config=merged,
                input_has_audio=input_has_audio,
            )
            command_summaries.append(" ".join(segment_cmd))
            proc = runner(segment_cmd)
            if proc.returncode != 0:
                stderr_tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-800:]
                return _failure_result(
                    reason=REASON_SEGMENT_RENDER_FAILED,
                    message=(
                        f"segment {index} ffmpeg failed with code {proc.returncode}: "
                        f"{stderr_tail or '(no output)'}"
                    ),
                    target_width=target_w,
                    target_height=target_h,
                    segments_rendered=index,
                    ffmpeg_command_summary=command_summaries[-1],
                )
            if not os.path.isfile(segment_path) or os.path.getsize(segment_path) == 0:
                return _failure_result(
                    reason=REASON_SEGMENT_RENDER_FAILED,
                    message=f"segment {index} output is missing or empty: {segment_path}",
                    target_width=target_w,
                    target_height=target_h,
                    segments_rendered=index,
                    ffmpeg_command_summary=command_summaries[-1],
                )
            segment_paths.append(segment_path)

        concat_list_path = os.path.join(segment_dir, "concat_list.txt")
        write_concat_list(concat_list_path, segment_paths)
        concat_cmd = build_concat_command(
            concat_list_path=concat_list_path,
            output_path=output_path,
            config=merged,
            input_has_audio=input_has_audio,
        )
        command_summaries.append(" ".join(concat_cmd))
        proc = runner(concat_cmd)
        if proc.returncode != 0:
            stderr_tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-800:]
            return _failure_result(
                reason=REASON_CONCAT_FAILED,
                message=(
                    f"concat ffmpeg failed with code {proc.returncode}: "
                    f"{stderr_tail or '(no output)'}"
                ),
                target_width=target_w,
                target_height=target_h,
                segments_rendered=len(segment_paths),
                ffmpeg_command_summary=command_summaries[-1],
            )
    finally:
        if bool(merged.get("cleanup_temp_segments", DEFAULT_CLEANUP_TEMP_SEGMENTS)):
            shutil.rmtree(segment_dir, ignore_errors=True)

    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        return _failure_result(
            reason=REASON_OUTPUT_MISSING,
            message=f"face-track render completed but output is missing: {output_path}",
            target_width=target_w,
            target_height=target_h,
            segments_rendered=len(segment_paths),
            ffmpeg_command_summary=command_summaries[-1] if command_summaries else None,
        )

    return FaceTrackRenderResult(
        ok=True,
        output_path=output_path,
        crop_renderer=str(merged["crop_renderer"]),
        segments_planned=plan_stats.segments_planned if plan_stats else len(segment_paths),
        segments_rendered=len(segment_paths),
        segments_merged=plan_stats.segments_merged if plan_stats else 0,
        unique_crop_rects_before_merge=(
            plan_stats.unique_crop_rects_before_merge if plan_stats else 0
        ),
        unique_crop_rects_after_merge=(
            plan_stats.unique_crop_rects_after_merge if plan_stats else 0
        ),
        segment_crop_change_threshold_px=(
            plan_stats.segment_crop_change_threshold_px
            if plan_stats
            else int(merged.get("segment_crop_change_threshold_px", DEFAULT_SEGMENT_CROP_CHANGE_THRESHOLD_PX))
        ),
        target_width=target_w,
        target_height=target_h,
        ffmpeg_command_summary=" | ".join(command_summaries),
    )


def face_track_render_metadata(
    *,
    render_result: FaceTrackRenderResult | None,
) -> dict[str, Any]:
    """Build safe metadata fields for platform_safe_format_v1."""
    metadata: dict[str, Any] = {
        "face_track_render_attempted": render_result is not None,
    }
    if render_result is None:
        return metadata

    metadata["face_track_rendered"] = render_result.ok
    metadata["crop_renderer"] = render_result.crop_renderer
    metadata["segments_planned"] = render_result.segments_planned
    metadata["segments_rendered"] = render_result.segments_rendered
    metadata["segments_merged"] = render_result.segments_merged
    metadata["unique_crop_rects_before_merge"] = render_result.unique_crop_rects_before_merge
    metadata["unique_crop_rects_after_merge"] = render_result.unique_crop_rects_after_merge
    metadata["segment_crop_change_threshold_px"] = render_result.segment_crop_change_threshold_px
    if render_result.reason:
        metadata["face_track_render_reason"] = render_result.reason
    if render_result.message and not render_result.ok:
        metadata["face_track_render_message"] = render_result.message
    return metadata


# ---------------------------------------------------------------------------
# Segment planning
# ---------------------------------------------------------------------------


def plan_render_segments_from_crop_path(
    samples: list[CropPathSample],
    *,
    clip_duration_sec: float,
    min_segment_duration_sec: float = DEFAULT_MIN_SEGMENT_DURATION_SEC,
    segment_crop_change_threshold_px: int = DEFAULT_SEGMENT_CROP_CHANGE_THRESHOLD_PX,
) -> tuple[list[RenderSegment], RenderSegmentPlanStats]:
    """Convert crop samples into merged render intervals with thresholding."""
    raw_segments = build_segment_intervals(
        samples,
        clip_duration_sec=clip_duration_sec,
        min_segment_duration_sec=min_segment_duration_sec,
    )
    segments_planned = len(raw_segments)
    unique_before = _count_unique_crop_rects(raw_segments)

    thresholded = _apply_crop_change_threshold(
        raw_segments,
        threshold_px=segment_crop_change_threshold_px,
    )
    merged = _merge_adjacent_equivalent_segments(thresholded)
    segments_rendered = len(merged)
    unique_after = _count_unique_crop_rects(merged)

    stats = RenderSegmentPlanStats(
        segments_planned=segments_planned,
        segments_rendered=segments_rendered,
        segments_merged=max(0, segments_planned - segments_rendered),
        unique_crop_rects_before_merge=unique_before,
        unique_crop_rects_after_merge=unique_after,
        segment_crop_change_threshold_px=segment_crop_change_threshold_px,
    )
    return merged, stats


def build_segment_intervals(
    samples: list[CropPathSample],
    *,
    clip_duration_sec: float,
    min_segment_duration_sec: float = DEFAULT_MIN_SEGMENT_DURATION_SEC,
) -> list[RenderSegment]:
    """Convert crop samples into contiguous render intervals."""
    if not samples:
        return []

    ordered = sorted(samples, key=lambda sample: sample.timestamp_sec)
    segments: list[RenderSegment] = []

    for index, sample in enumerate(ordered):
        start_sec = 0.0 if index == 0 else sample.timestamp_sec
        if index + 1 < len(ordered):
            end_sec = ordered[index + 1].timestamp_sec
        else:
            end_sec = clip_duration_sec

        if end_sec <= start_sec:
            end_sec = min(clip_duration_sec, start_sec + max(min_segment_duration_sec, 1e-3))
        if end_sec <= start_sec:
            continue

        segments.append(
            RenderSegment(
                start_sec=start_sec,
                end_sec=end_sec,
                crop=sample.crop,
                sample_index=sample.frame_index,
                held=sample.held,
            )
        )

    return _merge_short_segments(segments, min_segment_duration_sec=min_segment_duration_sec)


def validate_smoothed_crop_for_render(
    report: CropPathReport,
    *,
    target_width: int,
    target_height: int,
) -> str | None:
    """Return a reason code when the smoothed crop path cannot be rendered."""
    if not report.ok or not report.usable:
        return REASON_CROP_REPORT_NOT_USABLE
    if not report.samples:
        return REASON_NO_CROP_SAMPLES
    if report.source_width <= 0 or report.source_height <= 0:
        return REASON_INVALID_SOURCE_DIMENSIONS
    if target_width <= 0 or target_height <= 0:
        return REASON_INVALID_TARGET_DIMENSIONS

    target_aspect = target_width / target_height
    for sample in report.samples:
        if not _crop_rect_is_valid(
            sample.crop,
            source_width=report.source_width,
            source_height=report.source_height,
            target_aspect=report.target_aspect,
        ):
            return REASON_INVALID_CROP_RECT
        actual_aspect = sample.crop.width / sample.crop.height
        if abs(actual_aspect - target_aspect) > ASPECT_TOLERANCE:
            return REASON_INVALID_CROP_RECT

    return None


# ---------------------------------------------------------------------------
# FFmpeg command builders
# ---------------------------------------------------------------------------


def build_segment_command(
    *,
    input_path: str,
    output_path: str,
    segment: RenderSegment,
    target_w: int,
    target_h: int,
    config: dict[str, Any],
    input_has_audio: bool,
) -> list[str]:
    """Build ffmpeg args for one cropped/scaled segment."""
    crop = segment.crop
    vf = (
        f"crop={crop.width}:{crop.height}:{crop.x}:{crop.y},"
        f"scale={target_w}:{target_h}"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-ss",
        f"{segment.start_sec:.3f}",
        "-to",
        f"{segment.end_sec:.3f}",
        "-vf",
        vf,
        "-c:v",
        str(config.get("video_codec", "libx264")),
        "-preset",
        str(config.get("ffmpeg_preset", "veryfast")),
    ]
    if input_has_audio:
        cmd += ["-map", "0:v:0", "-map", "0:a?", "-c:a", str(config.get("audio_codec", "aac"))]
    else:
        cmd += ["-an"]
    cmd.append(output_path)
    return cmd


def build_concat_command(
    *,
    concat_list_path: str,
    output_path: str,
    config: dict[str, Any],
    input_has_audio: bool,
) -> list[str]:
    """Build ffmpeg args to concatenate rendered segments."""
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_list_path,
        "-c:v",
        str(config.get("video_codec", "libx264")),
        "-preset",
        str(config.get("ffmpeg_preset", "veryfast")),
    ]
    if input_has_audio:
        cmd += ["-c:a", str(config.get("audio_codec", "aac"))]
    else:
        cmd += ["-an"]
    cmd.append(output_path)
    return cmd


def write_concat_list(path: str, segment_paths: list[str]) -> None:
    """Write an FFmpeg concat demuxer list file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    lines: list[str] = []
    for segment_path in segment_paths:
        escaped = segment_path.replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        fh.write("\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _crop_rect_key(crop: CropRect) -> tuple[int, int, int, int]:
    return (crop.x, crop.y, crop.width, crop.height)


def _count_unique_crop_rects(segments: list[RenderSegment]) -> int:
    return len({_crop_rect_key(segment.crop) for segment in segments})


def _crops_within_threshold(
    left: CropRect,
    right: CropRect,
    *,
    threshold_px: int,
) -> bool:
    return (
        abs(left.x - right.x) <= threshold_px
        and abs(left.y - right.y) <= threshold_px
        and abs(left.width - right.width) <= threshold_px
        and abs(left.height - right.height) <= threshold_px
    )


def _apply_crop_change_threshold(
    segments: list[RenderSegment],
    *,
    threshold_px: int,
) -> list[RenderSegment]:
    if not segments:
        return []

    normalized: list[RenderSegment] = [segments[0]]
    for segment in segments[1:]:
        previous = normalized[-1]
        if _crops_within_threshold(previous.crop, segment.crop, threshold_px=threshold_px):
            normalized.append(
                RenderSegment(
                    start_sec=segment.start_sec,
                    end_sec=segment.end_sec,
                    crop=previous.crop,
                    sample_index=segment.sample_index,
                    held=segment.held,
                )
            )
        else:
            normalized.append(segment)
    return normalized


def _merge_adjacent_equivalent_segments(segments: list[RenderSegment]) -> list[RenderSegment]:
    if not segments:
        return []

    merged: list[RenderSegment] = [segments[0]]
    for segment in segments[1:]:
        previous = merged[-1]
        if _crop_rect_key(previous.crop) == _crop_rect_key(segment.crop):
            merged[-1] = RenderSegment(
                start_sec=previous.start_sec,
                end_sec=segment.end_sec,
                crop=previous.crop,
                sample_index=previous.sample_index,
                held=previous.held or segment.held,
            )
        else:
            merged.append(segment)
    return merged


def _merge_short_segments(
    segments: list[RenderSegment],
    *,
    min_segment_duration_sec: float,
) -> list[RenderSegment]:
    if not segments:
        return []

    merged: list[RenderSegment] = [segments[0]]
    for segment in segments[1:]:
        previous = merged[-1]
        duration = previous.end_sec - previous.start_sec
        if duration < min_segment_duration_sec:
            merged[-1] = RenderSegment(
                start_sec=previous.start_sec,
                end_sec=segment.end_sec,
                crop=previous.crop,
                sample_index=previous.sample_index,
                held=previous.held,
            )
        else:
            merged.append(segment)
    return merged


def _resolve_clip_duration(
    samples: list[CropPathSample],
    clip_duration_sec: float | None,
) -> float | None:
    if clip_duration_sec is not None and clip_duration_sec > 0:
        return float(clip_duration_sec)

    if not samples:
        return None

    ordered = sorted(samples, key=lambda sample: sample.timestamp_sec)
    if len(ordered) >= 2:
        gap = ordered[-1].timestamp_sec - ordered[-2].timestamp_sec
        if gap > 0:
            return ordered[-1].timestamp_sec + gap

    return ordered[-1].timestamp_sec + 0.5


def _crop_rect_is_valid(
    crop: CropRect,
    *,
    source_width: int,
    source_height: int,
    target_aspect: float,
) -> bool:
    if crop.width <= 0 or crop.height <= 0:
        return False
    if crop.x < 0 or crop.y < 0:
        return False
    if crop.x + crop.width > source_width:
        return False
    if crop.y + crop.height > source_height:
        return False

    actual_aspect = crop.width / crop.height
    return abs(actual_aspect - target_aspect) <= ASPECT_TOLERANCE


def _safe_render_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value))


def _default_run_ffmpeg(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=FFMPEG_TIMEOUT_SEC,
    )


def _failure_result(
    *,
    reason: str,
    message: str,
    target_width: int,
    target_height: int,
    segments_rendered: int = 0,
    segment_crop_change_threshold_px: int = DEFAULT_SEGMENT_CROP_CHANGE_THRESHOLD_PX,
    ffmpeg_command_summary: str | None = None,
) -> FaceTrackRenderResult:
    return FaceTrackRenderResult(
        ok=False,
        reason=reason,
        message=message,
        crop_renderer=DEFAULT_CROP_RENDERER,
        segments_rendered=segments_rendered,
        segment_crop_change_threshold_px=segment_crop_change_threshold_px,
        target_width=target_width,
        target_height=target_height,
        ffmpeg_command_summary=ffmpeg_command_summary,
    )
