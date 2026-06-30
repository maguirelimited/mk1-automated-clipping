"""Read Ops-UI-saved processing + post-processing settings from controls.json.

The Ops UI is the control plane: it writes ``controls.json`` with two blocks,
``processing_config`` and ``post_processing_config``. video-automation reads
those same shared blocks directly (no HTTP call to ops-ui) and translates them
into the config dicts the MK1 pipeline consumes:

    processing_config       -> sectioning_config + discovery_config
    post_processing_config  -> selection_config + conveyor_config (+ enabled flag)
    processing_pipeline_mode -> "legacy" | "mk1"

Resolution order for every field (first definite value wins):

    1. Ops UI saved value (controls.json block)
    2. environment variable
    3. safe built-in default (matching the service-side module defaults)

If the shared file is missing or invalid, this falls back cleanly to the
environment variable and then the default, so env-only setups keep working.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

PROCESSING_PIPELINE_MODES = ("legacy", "mk1")
DEFAULT_PIPELINE_MODE = "legacy"


def controls_file_path() -> Path:
    raw = os.environ.get("MK04_CONTROLS_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    # video-automation/scripts/processing_settings.py -> repo root is parents[2].
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "ops-ui" / "data" / "controls.json"


def _read_block(block_key: str) -> dict[str, Any]:
    path = controls_file_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    block = data.get(block_key)
    return block if isinstance(block, dict) else {}


def read_processing_config() -> dict[str, Any]:
    return _read_block("processing_config")


def read_post_processing_config() -> dict[str, Any]:
    return _read_block("post_processing_config")


# ---------------------------------------------------------------------------
# Field resolution
# ---------------------------------------------------------------------------


def _resolve_raw(saved: dict[str, Any], name: str, env_name: str) -> str:
    ui_value = saved.get(name)
    if ui_value is not None and str(ui_value).strip() != "":
        return str(ui_value).strip()
    env_value = os.environ.get(env_name, "").strip()
    if env_value:
        return env_value
    return ""


def _resolve_float(saved: dict[str, Any], name: str, env_name: str, default: float) -> float:
    raw = _resolve_raw(saved, name, env_name)
    if raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _resolve_int(saved: dict[str, Any], name: str, env_name: str, default: int) -> int:
    raw = _resolve_raw(saved, name, env_name)
    if raw == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _resolve_str(saved: dict[str, Any], name: str, env_name: str, default: str) -> str:
    raw = _resolve_raw(saved, name, env_name)
    return raw if raw != "" else default


def _resolve_bool(saved: dict[str, Any], name: str, env_name: str, default: bool) -> bool:
    raw = _resolve_raw(saved, name, env_name).lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return default


def _resolve_choice(
    saved: dict[str, Any], name: str, env_name: str, choices: tuple[str, ...], default: str
) -> str:
    raw = _resolve_raw(saved, name, env_name)
    return raw if raw in choices else default


# ---------------------------------------------------------------------------
# Public resolvers
# ---------------------------------------------------------------------------


def resolve_pipeline_mode(per_run: str | None = None) -> str:
    """Resolve processing pipeline mode: per-run -> UI -> env -> default."""
    candidate = (per_run or "").strip().lower()
    if candidate in PROCESSING_PIPELINE_MODES:
        return candidate
    saved = read_processing_config()
    return _resolve_choice(
        saved,
        "processing_pipeline_mode",
        "PROCESSING_PIPELINE_MODE",
        PROCESSING_PIPELINE_MODES,
        DEFAULT_PIPELINE_MODE,
    )


def resolve_sectioning_config() -> dict[str, Any]:
    """Build a ``TranscriptSectioningConfig``-compatible dict."""
    saved = read_processing_config()
    return {
        "target_section_duration_sec": _resolve_float(
            saved, "section_target_duration_sec", "PROCESSING_SECTION_TARGET_DURATION_SEC", 300.0
        ),
        "max_section_duration_sec": _resolve_float(
            saved, "section_max_duration_sec", "PROCESSING_SECTION_MAX_DURATION_SEC", 420.0
        ),
        "overlap_sec": _resolve_float(
            saved, "section_overlap_sec", "PROCESSING_SECTION_OVERLAP_SEC", 30.0
        ),
        "min_section_duration_sec": _resolve_float(
            saved, "section_min_duration_sec", "PROCESSING_SECTION_MIN_DURATION_SEC", 60.0
        ),
    }


def resolve_discovery_config() -> dict[str, Any]:
    """Build a ``CandidateDiscoveryConfig``-compatible dict."""
    saved = read_processing_config()
    return {
        "max_candidates_per_section": _resolve_int(
            saved, "max_candidates_per_section", "PROCESSING_MAX_CANDIDATES_PER_SECTION", 3
        ),
        "min_candidate_duration_sec": _resolve_float(
            saved, "min_candidate_duration_sec", "PROCESSING_MIN_CANDIDATE_DURATION_SEC", 15.0
        ),
        "max_candidate_duration_sec": _resolve_float(
            saved, "max_candidate_duration_sec", "PROCESSING_MAX_CANDIDATE_DURATION_SEC", 120.0
        ),
        "fail_fast": _resolve_bool(
            saved, "discovery_fail_fast", "PROCESSING_DISCOVERY_FAIL_FAST", False
        ),
    }


def resolve_post_processing_enabled() -> bool:
    saved = read_post_processing_config()
    return _resolve_bool(
        saved, "post_processing_enabled", "POST_PROCESSING_ENABLED", True
    )


_SELECTION_MODES = (
    "maximum_quality",
    "balanced",
    "growth",
    "maximum_data_collection",
    "custom",
)


def resolve_selection_config() -> dict[str, Any]:
    """Build a selection_gate_v1 config dict (merged over the mode preset)."""
    saved = read_post_processing_config()
    return {
        "selection_mode": _resolve_choice(
            saved, "selection_mode", "POST_PROCESSING_SELECTION_MODE", _SELECTION_MODES, "balanced"
        ),
        "max_clips": _resolve_int(saved, "max_clips", "POST_PROCESSING_MAX_CLIPS", 6),
        "reserve_count": _resolve_int(saved, "reserve_count", "POST_PROCESSING_RESERVE_COUNT", 3),
        "min_overall_potential": _resolve_float(
            saved, "min_overall_potential", "POST_PROCESSING_MIN_OVERALL_POTENTIAL", 7.0
        ),
        "min_confidence": _resolve_float(
            saved, "min_confidence", "POST_PROCESSING_MIN_CONFIDENCE", 0.6
        ),
        "min_duration_sec": _resolve_float(
            saved, "min_duration_sec", "POST_PROCESSING_MIN_DURATION_SEC", 15.0
        ),
        "max_duration_sec": _resolve_float(
            saved, "max_duration_sec", "POST_PROCESSING_MAX_DURATION_SEC", 120.0
        ),
        "respect_candidate_warnings": _resolve_bool(
            saved, "respect_candidate_warnings", "POST_PROCESSING_RESPECT_CANDIDATE_WARNINGS", True
        ),
        "respect_transcript_quality_flags": _resolve_bool(
            saved,
            "respect_transcript_quality_flags",
            "POST_PROCESSING_RESPECT_TRANSCRIPT_QUALITY_FLAGS",
            True,
        ),
        "allow_reserve_candidates": _resolve_bool(
            saved, "allow_reserve_candidates", "POST_PROCESSING_ALLOW_RESERVE_CANDIDATES", True
        ),
    }


_BACKGROUND_MODES = ("blurred", "solid")


def resolve_conveyor_config() -> dict[str, Any]:
    """Build a flat conveyor config consumed by the format + caption modules.

    Keys here use the *module* config names (target_width, font_size, ...) so
    they are picked up directly by platform_safe_format_v1 and
    intelligent_captions_v1 through the shared conveyor config.
    """
    saved = read_post_processing_config()
    return {
        # platform_safe_format_v1
        "target_width": _resolve_int(
            saved, "format_target_width", "POST_PROCESSING_FORMAT_TARGET_WIDTH", 1080
        ),
        "target_height": _resolve_int(
            saved, "format_target_height", "POST_PROCESSING_FORMAT_TARGET_HEIGHT", 1920
        ),
        "background_mode": _resolve_choice(
            saved,
            "format_background_mode",
            "POST_PROCESSING_FORMAT_BACKGROUND_MODE",
            _BACKGROUND_MODES,
            "blurred",
        ),
        "background_blur": _resolve_str(
            saved, "format_background_blur", "POST_PROCESSING_FORMAT_BACKGROUND_BLUR", "20:1"
        ),
        "ffmpeg_preset": _resolve_str(
            saved, "format_ffmpeg_preset", "POST_PROCESSING_FORMAT_FFMPEG_PRESET", "veryfast"
        ),
        "video_codec": _resolve_str(
            saved, "format_video_codec", "POST_PROCESSING_FORMAT_VIDEO_CODEC", "libx264"
        ),
        "audio_codec": _resolve_str(
            saved, "format_audio_codec", "POST_PROCESSING_FORMAT_AUDIO_CODEC", "aac"
        ),
        # intelligent_captions_v1
        "font_family": _resolve_str(
            saved, "captions_font_family", "POST_PROCESSING_CAPTIONS_FONT_FAMILY", "Arial"
        ),
        "font_size": _resolve_int(
            saved, "captions_font_size", "POST_PROCESSING_CAPTIONS_FONT_SIZE", 64
        ),
        "max_lines": _resolve_int(
            saved, "captions_max_lines", "POST_PROCESSING_CAPTIONS_MAX_LINES", 2
        ),
        "max_chars_per_line": _resolve_int(
            saved, "captions_max_chars_per_line", "POST_PROCESSING_CAPTIONS_MAX_CHARS_PER_LINE", 32
        ),
        "max_chars_per_caption": _resolve_int(
            saved,
            "captions_max_chars_per_caption",
            "POST_PROCESSING_CAPTIONS_MAX_CHARS_PER_CAPTION",
            42,
        ),
        "enable_keyword_highlighting": _resolve_bool(
            saved,
            "captions_enable_keyword_highlighting",
            "POST_PROCESSING_CAPTIONS_ENABLE_KEYWORD_HIGHLIGHTING",
            False,
        ),
        "highlight_numbers": _resolve_bool(
            saved, "captions_highlight_numbers", "POST_PROCESSING_CAPTIONS_HIGHLIGHT_NUMBERS", False
        ),
    }
