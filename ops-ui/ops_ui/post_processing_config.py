"""Post-processing configuration contract for the Ops UI.

These operator-facing settings control the MK1 post-processing pipeline that
turns a raw candidate pool into finished clips: the selection gate and the fixed
universal conveyor (render -> platform-safe format -> intelligent captions ->
validation -> metadata). They are persisted into ``controls.json`` under a
``post_processing_config`` block which ``video-automation`` reads via
``post_processing_settings.py``.

Defaults here mirror the service-side defaults exactly:
- selection gate: ``selection_gate_v1._MODE_DEFAULTS["balanced"]``
- platform-safe format: ``platform_safe_format_v1._DEFAULT_CONFIG``
- intelligent captions: ``intelligent_captions_v1._DEFAULT_CONFIG``
"""

from __future__ import annotations

from typing import Any

from .settings_fields import (
    ConfigField,
    effective_config as _effective_config,
    fields_view as _fields_view,
    parse_form as _parse_form,
    source_for as _source_for,
)

POST_PROCESSING_CONFIG_STORE_PREFIX = "post_processing_config."
POST_PROCESSING_CONFIG_FILE_KEY = "post_processing_config"

SELECTION_MODES = (
    "maximum_quality",
    "balanced",
    "growth",
    "maximum_data_collection",
    "custom",
)

BACKGROUND_MODES = ("blurred", "solid")


POST_PROCESSING_CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        name="post_processing_enabled",
        label="Run post-processing",
        kind="bool",
        default=True,
        env_var="POST_PROCESSING_ENABLED",
        group="Pipeline",
        help=(
            "When on (and processing pipeline mode is mk1), selected candidates "
            "are run through the universal conveyor to produce finished clips. "
            "When off, processing stops at the raw candidate pool."
        ),
    ),
    # -- Selection gate --
    ConfigField(
        name="selection_mode",
        label="Selection mode",
        kind="choice",
        default="balanced",
        env_var="POST_PROCESSING_SELECTION_MODE",
        choices=SELECTION_MODES,
        group="Selection gate",
        help=(
            "Preset thresholds for choosing which raw candidates become clips. "
            "maximum_quality (fewest/strongest) .. maximum_data_collection "
            "(most/loosest). custom uses the explicit values below."
        ),
    ),
    ConfigField(
        name="max_clips",
        label="Max clips",
        kind="int",
        default=6,
        env_var="POST_PROCESSING_MAX_CLIPS",
        minimum=1.0,
        maximum=50.0,
        group="Selection gate",
        help="Maximum number of clips selected per job.",
    ),
    ConfigField(
        name="reserve_count",
        label="Reserve count",
        kind="int",
        default=3,
        env_var="POST_PROCESSING_RESERVE_COUNT",
        minimum=0.0,
        maximum=50.0,
        group="Selection gate",
        help="Valid-but-not-selected candidates kept in reserve for later analysis.",
    ),
    ConfigField(
        name="min_overall_potential",
        label="Min overall potential (0-10)",
        kind="float",
        default=7.0,
        env_var="POST_PROCESSING_MIN_OVERALL_POTENTIAL",
        minimum=0.0,
        maximum=10.0,
        group="Selection gate",
        help="Candidates scoring below this overall potential are rejected.",
    ),
    ConfigField(
        name="min_confidence",
        label="Min confidence (0-1)",
        kind="float",
        default=0.6,
        env_var="POST_PROCESSING_MIN_CONFIDENCE",
        minimum=0.0,
        maximum=1.0,
        group="Selection gate",
        help="Candidates below this discovery confidence are rejected.",
    ),
    ConfigField(
        name="min_duration_sec",
        label="Min clip duration (s)",
        kind="float",
        default=15.0,
        env_var="POST_PROCESSING_MIN_DURATION_SEC",
        minimum=0.0,
        maximum=600.0,
        group="Selection gate",
        help="Candidates shorter than this are rejected by the selection gate.",
    ),
    ConfigField(
        name="max_duration_sec",
        label="Max clip duration (s)",
        kind="float",
        default=120.0,
        env_var="POST_PROCESSING_MAX_DURATION_SEC",
        minimum=0.0,
        maximum=1800.0,
        group="Selection gate",
        help="Candidates longer than this are rejected (must be >= min).",
    ),
    ConfigField(
        name="respect_candidate_warnings",
        label="Respect candidate warnings",
        kind="bool",
        default=True,
        env_var="POST_PROCESSING_RESPECT_CANDIDATE_WARNINGS",
        group="Selection gate",
        help="When on, blocking candidate warnings can reject a candidate.",
    ),
    ConfigField(
        name="respect_transcript_quality_flags",
        label="Respect transcript quality flags",
        kind="bool",
        default=True,
        env_var="POST_PROCESSING_RESPECT_TRANSCRIPT_QUALITY_FLAGS",
        group="Selection gate",
        help="When on, blocking transcript-quality flags can reject a candidate.",
    ),
    ConfigField(
        name="allow_reserve_candidates",
        label="Allow reserve candidates",
        kind="bool",
        default=True,
        env_var="POST_PROCESSING_ALLOW_RESERVE_CANDIDATES",
        group="Selection gate",
        help="When off, no reserve set is produced (only selected or rejected).",
    ),
    # -- Platform-safe format module --
    ConfigField(
        name="format_target_width",
        label="Output width (px)",
        kind="int",
        default=1080,
        env_var="POST_PROCESSING_FORMAT_TARGET_WIDTH",
        minimum=16.0,
        maximum=8192.0,
        group="Platform-safe format",
        help="Vertical export width. Default 1080 for 9:16 short-form.",
    ),
    ConfigField(
        name="format_target_height",
        label="Output height (px)",
        kind="int",
        default=1920,
        env_var="POST_PROCESSING_FORMAT_TARGET_HEIGHT",
        minimum=16.0,
        maximum=8192.0,
        group="Platform-safe format",
        help="Vertical export height. Default 1920 for 9:16 short-form.",
    ),
    ConfigField(
        name="format_background_mode",
        label="Background mode",
        kind="choice",
        default="blurred",
        env_var="POST_PROCESSING_FORMAT_BACKGROUND_MODE",
        choices=BACKGROUND_MODES,
        group="Platform-safe format",
        help="How the 9:16 frame is filled behind the source video.",
    ),
    ConfigField(
        name="format_background_blur",
        label="Background blur",
        kind="text",
        default="20:1",
        env_var="POST_PROCESSING_FORMAT_BACKGROUND_BLUR",
        group="Platform-safe format",
        help="ffmpeg boxblur strength (luma:chroma) for the blurred background.",
    ),
    ConfigField(
        name="format_ffmpeg_preset",
        label="ffmpeg preset",
        kind="text",
        default="veryfast",
        env_var="POST_PROCESSING_FORMAT_FFMPEG_PRESET",
        group="Platform-safe format",
        help="libx264 encode preset (e.g. ultrafast, veryfast, medium).",
    ),
    ConfigField(
        name="format_video_codec",
        label="Video codec",
        kind="text",
        default="libx264",
        env_var="POST_PROCESSING_FORMAT_VIDEO_CODEC",
        group="Platform-safe format",
        help="ffmpeg video codec for formatted output.",
    ),
    ConfigField(
        name="format_audio_codec",
        label="Audio codec",
        kind="text",
        default="aac",
        env_var="POST_PROCESSING_FORMAT_AUDIO_CODEC",
        group="Platform-safe format",
        help="ffmpeg audio codec for formatted output.",
    ),
    # -- Intelligent captions module --
    ConfigField(
        name="captions_font_family",
        label="Caption font",
        kind="text",
        default="Arial",
        env_var="POST_PROCESSING_CAPTIONS_FONT_FAMILY",
        group="Captions",
        help="Font family for burned-in captions.",
    ),
    ConfigField(
        name="captions_font_size",
        label="Caption font size",
        kind="int",
        default=64,
        env_var="POST_PROCESSING_CAPTIONS_FONT_SIZE",
        minimum=8.0,
        maximum=256.0,
        group="Captions",
        help="Caption font size in points.",
    ),
    ConfigField(
        name="captions_max_lines",
        label="Caption max lines",
        kind="int",
        default=2,
        env_var="POST_PROCESSING_CAPTIONS_MAX_LINES",
        minimum=1.0,
        maximum=6.0,
        group="Captions",
        help="Maximum lines shown per caption block.",
    ),
    ConfigField(
        name="captions_max_chars_per_line",
        label="Caption chars per line",
        kind="int",
        default=32,
        env_var="POST_PROCESSING_CAPTIONS_MAX_CHARS_PER_LINE",
        minimum=8.0,
        maximum=120.0,
        group="Captions",
        help="Maximum characters per caption line before wrapping.",
    ),
    ConfigField(
        name="captions_max_chars_per_caption",
        label="Caption chars per block",
        kind="int",
        default=42,
        env_var="POST_PROCESSING_CAPTIONS_MAX_CHARS_PER_CAPTION",
        minimum=8.0,
        maximum=240.0,
        group="Captions",
        help="Maximum total characters per caption block.",
    ),
    ConfigField(
        name="captions_enable_keyword_highlighting",
        label="Highlight keywords",
        kind="bool",
        default=False,
        env_var="POST_PROCESSING_CAPTIONS_ENABLE_KEYWORD_HIGHLIGHTING",
        group="Captions",
        help="When on, configured keywords are visually highlighted in captions.",
    ),
    ConfigField(
        name="captions_highlight_numbers",
        label="Highlight numbers",
        kind="bool",
        default=False,
        env_var="POST_PROCESSING_CAPTIONS_HIGHLIGHT_NUMBERS",
        group="Captions",
        help="When on (and keyword highlighting is enabled), numbers are highlighted.",
    ),
)

POST_PROCESSING_CONFIG_FIELDS_BY_NAME = {f.name: f for f in POST_PROCESSING_CONFIG_FIELDS}


def effective_config(saved: dict[str, str]) -> dict[str, Any]:
    return _effective_config(POST_PROCESSING_CONFIG_FIELDS, saved)


def source_for(field_name: str, saved: dict[str, str]) -> str:
    return _source_for(POST_PROCESSING_CONFIG_FIELDS_BY_NAME, field_name, saved)


def parse_form(form: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    return _parse_form(POST_PROCESSING_CONFIG_FIELDS, form)


def fields_view(saved: dict[str, str]) -> list[dict[str, Any]]:
    return _fields_view(
        POST_PROCESSING_CONFIG_FIELDS, POST_PROCESSING_CONFIG_FIELDS_BY_NAME, saved
    )
