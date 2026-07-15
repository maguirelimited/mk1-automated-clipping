"""Blurred-background 9:16 fallback renderer for MK1 platform-safe formatting."""

from __future__ import annotations

from typing import Any


def build_blur_background_command(
    *,
    input_path: str,
    output_path: str,
    target_w: int,
    target_h: int,
    config: dict[str, Any],
    input_has_audio: bool,
) -> list[str]:
    """Build ffmpeg args for the blurred-background + centred-foreground pass.

    Filter graph:
        [0:v] → scale to fill target canvas, crop to exact size, boxblur → [bg]
        [0:v] → scale to fit inside target canvas (no stretch)            → [fg]
        [bg][fg] → overlay centred                                         → [vout]
    """
    blur = str(config.get("background_blur", "20:1"))
    video_codec = str(config.get("video_codec", "libx264"))
    audio_codec = str(config.get("audio_codec", "aac"))
    preset = str(config.get("ffmpeg_preset", "veryfast"))

    bg_filter = (
        f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},"
        f"boxblur={blur}[bg]"
    )
    fg_filter = (
        f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg]"
    )
    overlay_filter_named = "[bg][fg]overlay=(W-w)/2:(H-h)/2[vout]"
    filter_complex = f"{bg_filter};{fg_filter};{overlay_filter_named}"

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-c:v", video_codec,
        "-preset", preset,
    ]

    if input_has_audio:
        cmd += ["-map", "0:a?", "-c:a", audio_codec]
    else:
        cmd += ["-an"]

    cmd.append(output_path)
    return cmd
