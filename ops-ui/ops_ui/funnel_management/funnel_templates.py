"""Built-in funnel templates for canonical funnel creation (Funnel Management MK1).

Templates are creation tools only — not persisted funnel configuration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .create_defaults import BASELINE_TEMPLATE_ID
from .schema import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_PLATFORMS,
    CanonicalFunnel,
    CanonicalFunnelSchemaError,
    load_canonical_funnel,
)

_TEMPLATE_ID_RE = re.compile(r"^[a-z0-9_]+$")
_FUNNEL_ID_RE = re.compile(r"^[a-z0-9_]+$")

_ALL_PLATFORMS_FALSE = {name: False for name in sorted(ALLOWED_PLATFORMS)}


class FunnelTemplateError(ValueError):
    """Raised when a template lookup or draft generation request is invalid."""


@dataclass(frozen=True)
class FunnelTemplate:
    template_id: str
    display_name: str
    description: str
    category: str
    source_type: str
    defaults: dict[str, Any]


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_template_id(template_id: str) -> str:
    clean = str(template_id or "").strip()
    if not clean or not _TEMPLATE_ID_RE.match(clean):
        raise FunnelTemplateError(
            "template_id must contain only lowercase letters, numbers, and underscores"
        )
    return clean


def _validate_funnel_id(funnel_id: str) -> str:
    clean = str(funnel_id or "").strip()
    if not clean or not _FUNNEL_ID_RE.match(clean):
        raise FunnelTemplateError(
            "funnel_id must contain only lowercase letters, numbers, and underscores"
        )
    if len(clean) > 128:
        raise FunnelTemplateError("funnel_id is too long")
    return clean


def _validate_environment(environment: str) -> str:
    env = str(environment or "").strip().lower()
    if env not in ALLOWED_ENVIRONMENTS:
        raise FunnelTemplateError(
            f"environment must be one of {', '.join(sorted(ALLOWED_ENVIRONMENTS))}, got {environment!r}"
        )
    return env


def _platform_flags(enabled: dict[str, bool]) -> dict[str, bool]:
    flags = dict(_ALL_PLATFORMS_FALSE)
    for name, value in enabled.items():
        if name in ALLOWED_PLATFORMS:
            flags[name] = bool(value)
    return flags


def _build_template(
    *,
    template_id: str,
    display_name: str,
    description: str,
    category: str,
    source_type: str,
    acquisition: dict[str, Any],
    processing: dict[str, Any],
    distribution: dict[str, Any],
) -> FunnelTemplate:
    return FunnelTemplate(
        template_id=template_id,
        display_name=display_name,
        description=description,
        category=category,
        source_type=source_type,
        defaults={
            "category": category,
            "source_type": source_type,
            "acquisition": acquisition,
            "processing": processing,
            "distribution": distribution,
        },
    )


_BUILTIN_TEMPLATES: dict[str, FunnelTemplate] = {
    "baseline_stream_clips": _build_template(
        template_id="baseline_stream_clips",
        display_name="Baseline Stream Clips",
        description=(
            "Minimal dev/test funnel — download a source and produce rough clips. "
            "Tune prompts and quality later per niche."
        ),
        category="general",
        source_type="youtube_channel",
        acquisition={
            "min_duration_minutes": 5,
            "max_duration_minutes": 180,
            "max_downloads_per_run": 1,
        },
        processing={
            "ai_rule_profile": "business",
            "max_clips": 2,
            "min_clip_duration_sec": 10,
            "max_clip_duration_sec": 90,
            "max_overlap_sec": 2,
            "delivery_mode": "pull_from_output_endpoint",
            "platforms": _platform_flags({"youtube_shorts": True}),
        },
        distribution={
            "posting_enabled": False,
            "posting_mode": "disabled",
            "target_platforms": [],
            "channel_routes": [],
        },
    ),
    "youtube_podcast_basic": _build_template(
        template_id="youtube_podcast_basic",
        display_name="YouTube Podcast Basic",
        description="Long-form YouTube podcast or interview clipping funnel with conservative defaults.",
        category="business",
        source_type="youtube_channel",
        acquisition={
            "min_duration_minutes": 20,
            "max_duration_minutes": 180,
            "max_downloads_per_run": 1,
        },
        processing={
            "ai_rule_profile": "business",
            "max_clips": 6,
            "min_clip_duration_sec": 20,
            "max_clip_duration_sec": 90,
            "max_overlap_sec": 5,
            "delivery_mode": "handoff",
            "platforms": _platform_flags(
                {
                    "youtube_shorts": True,
                    "tiktok": True,
                    "instagram_reels": True,
                    "facebook_reels": False,
                    "x": False,
                }
            ),
        },
        distribution={
            "posting_enabled": False,
            "posting_mode": "manual_review",
            "target_platforms": ["youtube_shorts", "tiktok", "instagram_reels"],
            "channel_routes": [],
        },
    ),
    "youtube_channel_basic": _build_template(
        template_id="youtube_channel_basic",
        display_name="YouTube Channel Basic",
        description="Generic YouTube channel-based clipping funnel with neutral defaults.",
        category="general",
        source_type="youtube_channel",
        acquisition={
            "min_duration_minutes": 15,
            "max_duration_minutes": 120,
            "max_downloads_per_run": 1,
        },
        processing={
            "ai_rule_profile": "business",
            "max_clips": 4,
            "min_clip_duration_sec": 15,
            "max_clip_duration_sec": 60,
            "max_overlap_sec": 3,
            "delivery_mode": "pull_from_output_endpoint",
            "platforms": _platform_flags({"youtube_shorts": True}),
        },
        distribution={
            "posting_enabled": False,
            "posting_mode": "manual_review",
            "target_platforms": ["youtube_shorts"],
            "channel_routes": [],
        },
    ),
    "youtube_playlist_basic": _build_template(
        template_id="youtube_playlist_basic",
        display_name="YouTube Playlist Basic",
        description="YouTube playlist-based clipping funnel with conservative acquisition defaults.",
        category="general",
        source_type="youtube_playlist",
        acquisition={
            "min_duration_minutes": 10,
            "max_duration_minutes": 90,
            "max_downloads_per_run": 1,
        },
        processing={
            "ai_rule_profile": "business",
            "max_clips": 4,
            "min_clip_duration_sec": 15,
            "max_clip_duration_sec": 60,
            "max_overlap_sec": 2,
            "delivery_mode": "pull_from_output_endpoint",
            "platforms": _platform_flags({"youtube_shorts": True}),
        },
        distribution={
            "posting_enabled": False,
            "posting_mode": "manual_review",
            "target_platforms": ["youtube_shorts"],
            "channel_routes": [],
        },
    ),
}


def list_funnel_templates() -> list[FunnelTemplate]:
    """Return built-in funnel templates with the baseline template first."""
    ordered = [_BUILTIN_TEMPLATES[key] for key in sorted(_BUILTIN_TEMPLATES)]
    baseline = _BUILTIN_TEMPLATES.get(BASELINE_TEMPLATE_ID)
    if baseline is None:
        return ordered
    return [baseline] + [template for template in ordered if template.template_id != BASELINE_TEMPLATE_ID]


def get_funnel_template(template_id: str) -> FunnelTemplate:
    """Look up one built-in funnel template by ID."""
    clean_id = _validate_template_id(template_id)
    template = _BUILTIN_TEMPLATES.get(clean_id)
    if template is None:
        raise FunnelTemplateError(f"Unknown funnel template: {clean_id!r}")
    return template


def build_funnel_from_template(
    template_id: str,
    *,
    funnel_id: str,
    display_name: str,
    environment: str,
    description: str | None = None,
    category: str | None = None,
    sources: list[dict[str, Any]] | None = None,
    channel_routes: list[dict[str, Any]] | None = None,
    target_platforms: list[str] | None = None,
) -> CanonicalFunnel:
    """Build a draft canonical funnel from a built-in template (not saved)."""
    template = get_funnel_template(template_id)
    clean_funnel_id = _validate_funnel_id(funnel_id)
    clean_display_name = str(display_name or "").strip()
    if not clean_display_name:
        raise FunnelTemplateError("display_name must be a non-empty string")
    clean_environment = _validate_environment(environment)

    defaults = template.defaults
    acquisition_defaults = dict(defaults.get("acquisition") or {})
    processing_defaults = dict(defaults.get("processing") or {})
    distribution_defaults = dict(defaults.get("distribution") or {})

    resolved_category = (category or defaults.get("category") or template.category)
    resolved_description = description
    resolved_sources = list(sources or [])
    resolved_routes = list(channel_routes if channel_routes is not None else distribution_defaults.get("channel_routes") or [])
    resolved_targets = list(
        target_platforms
        if target_platforms is not None
        else distribution_defaults.get("target_platforms") or []
    )

    posting_enabled = bool(distribution_defaults.get("posting_enabled", False))
    if resolved_routes and posting_enabled:
        posting_enabled = False

    platforms = processing_defaults.get("platforms")
    if not isinstance(platforms, dict):
        platforms = _platform_flags({})

    now = _utc_now_iso()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "identity": {
            "funnel_id": clean_funnel_id,
            "display_name": clean_display_name,
            "description": resolved_description,
            "category": str(resolved_category).strip() or None,
            "enabled": False,
            "environment": clean_environment,
            "status": "draft",
            "template_source": template.template_id,
            "created_at": now,
            "updated_at": now,
            "operator_note": None,
        },
        "acquisition": {
            "source_type": defaults.get("source_type") or template.source_type,
            "sources": resolved_sources,
            "min_duration_minutes": acquisition_defaults.get("min_duration_minutes"),
            "max_duration_minutes": acquisition_defaults.get("max_duration_minutes"),
            "max_downloads_per_run": acquisition_defaults.get("max_downloads_per_run"),
        },
        "processing": {
            "pipeline_profile": clean_funnel_id,
            "ai_rules": {
                "ai_rule_profile": processing_defaults.get("ai_rule_profile"),
            },
            "selection": {
                "max_clips": processing_defaults.get("max_clips"),
                "min_clip_duration_sec": processing_defaults.get("min_clip_duration_sec"),
                "max_clip_duration_sec": processing_defaults.get("max_clip_duration_sec"),
                "max_overlap_sec": processing_defaults.get("max_overlap_sec"),
            },
            "output": {
                "filename_prefix": clean_funnel_id.replace("_", "-")[:128],
                "delivery_mode": processing_defaults.get("delivery_mode"),
            },
            "platforms": platforms,
        },
        "distribution": {
            "posting_enabled": posting_enabled,
            "posting_mode": distribution_defaults.get("posting_mode", "manual_review"),
            "target_platforms": resolved_targets,
            "channel_routes": resolved_routes,
        },
        "mappings": {
            "config_manager_funnel_id": None,
        },
    }

    try:
        return load_canonical_funnel(payload)
    except CanonicalFunnelSchemaError as exc:
        raise FunnelTemplateError(f"Generated funnel failed schema validation: {exc}") from exc
