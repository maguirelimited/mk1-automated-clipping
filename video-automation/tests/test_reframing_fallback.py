"""Focused tests for reframing.fallback — blurred-background FFmpeg command."""

from __future__ import annotations

import os
import sys

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from reframing.config import FORMAT_STRATEGY_BLURRED_BACKGROUND, resolve_reframe_plan
from reframing.fallback import build_blur_background_command


_BASE_CONFIG = {
    "background_blur": "20:1",
    "video_codec": "libx264",
    "audio_codec": "aac",
    "ffmpeg_preset": "veryfast",
}


def _cmd_str(cmd: list[str]) -> str:
    return " ".join(cmd)


def test_build_blur_background_command_filter_graph():
    cmd = build_blur_background_command(
        input_path="/in.mp4",
        output_path="/out.mp4",
        target_w=1080,
        target_h=1920,
        config=_BASE_CONFIG,
        input_has_audio=True,
    )
    cmd_str = _cmd_str(cmd)
    assert "force_original_aspect_ratio=increase" in cmd_str
    assert "force_original_aspect_ratio=decrease" in cmd_str
    assert "boxblur=20:1" in cmd_str
    assert "overlay=(W-w)/2:(H-h)/2[vout]" in cmd_str
    assert "-map [vout]" in cmd_str
    assert "-map 0:a?" in cmd_str
    assert "-c:a aac" in cmd_str


def test_build_blur_background_command_without_audio():
    cmd = build_blur_background_command(
        input_path="/in.mp4",
        output_path="/out.mp4",
        target_w=1080,
        target_h=1920,
        config=_BASE_CONFIG,
        input_has_audio=False,
    )
    cmd_str = _cmd_str(cmd)
    assert "-an" in cmd_str
    assert "0:a?" not in cmd_str


def test_blur_plan_uses_blurred_background_strategy():
    plan = resolve_reframe_plan({"reframe_mode": "blur_background"})
    assert plan["format_strategy"] == FORMAT_STRATEGY_BLURRED_BACKGROUND
    assert plan["use_blur_fallback"] is True


def test_auto_without_test_flag_skips_face_pipeline():
    plan = resolve_reframe_plan({"reframe_mode": "auto"})
    assert plan["attempt_face_pipeline"] is False
    assert plan["face_track_skip_reason"] == "face_track_test_disabled"


def test_auto_with_test_flag_attempts_face_pipeline():
    plan = resolve_reframe_plan({"reframe_mode": "auto", "face_track_test_enabled": True})
    assert plan["attempt_face_pipeline"] is True
    assert plan["face_track_test_enabled"] is True


def test_fallback_command_matches_platform_safe_format_wrapper():
    from platform_safe_format_v1 import _build_format_command

    kwargs = {
        "input_path": "/in.mp4",
        "output_path": "/out.mp4",
        "target_w": 1080,
        "target_h": 1920,
        "config": _BASE_CONFIG,
        "input_has_audio": True,
    }
    direct = build_blur_background_command(**kwargs)
    wrapped = _build_format_command(**kwargs)
    assert direct == wrapped
