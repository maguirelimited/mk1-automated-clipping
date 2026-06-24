from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import MetadataResult
from .time_utils import parse_iso_datetime, to_utc_iso

CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
WHITESPACE_RE = re.compile(r"\s+")
HASHTAG_RE = re.compile(r"^#[A-Za-z0-9_]{1,80}$")

YOUTUBE_TITLE_LIMIT = 100
YOUTUBE_DESCRIPTION_LIMIT = 5000
YOUTUBE_HASHTAG_LIMIT = 15
PLATFORM_LIMITS: dict[str, dict[str, int]] = {
    "youtube_shorts": {
        "title": YOUTUBE_TITLE_LIMIT,
        "description": YOUTUBE_DESCRIPTION_LIMIT,
        "hashtags": YOUTUBE_HASHTAG_LIMIT,
    },
    "x": {"title": 280, "description": 280, "hashtags": 8},
    "instagram_reels": {"title": 125, "description": 2200, "hashtags": 30},
    "facebook_reels": {"title": 255, "description": 63206, "hashtags": 30},
}


def clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = CONTROL_CHARS_RE.sub(" ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def normalize_hashtag(value: str) -> str | None:
    tag = clean_text(value)
    if not tag:
        return None
    if not tag.startswith("#"):
        tag = f"#{tag}"
    tag = re.sub(r"[^#A-Za-z0-9_]", "", tag)
    if not HASHTAG_RE.fullmatch(tag):
        return None
    return tag


def normalize_hashtags(values: list[Any], *, max_hashtags: int) -> list[str]:
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        tag = normalize_hashtag(value)
        if tag and tag.lower() not in {x.lower() for x in out}:
            out.append(tag)
        if len(out) >= max_hashtags:
            break
    return out


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip()


def _metadata_style(profile: dict[str, Any]) -> dict[str, Any]:
    raw = profile.get("metadata_style")
    return dict(raw) if isinstance(raw, dict) else {}


def normalize_metadata(
    source_clip: dict[str, Any],
    profile: dict[str, Any],
    *,
    publish_at: str | None = None,
) -> MetadataResult:
    style = _metadata_style(profile)
    platform = str(profile.get("platform") or "youtube_shorts")
    limits = PLATFORM_LIMITS.get(platform, PLATFORM_LIMITS["youtube_shorts"])
    issues: list[str] = []

    base_title = clean_text(source_clip.get("title") or source_clip.get("hook") or "")
    if not base_title:
        base_title = "Untitled clip"
        issues.append("missing_source_title")
    title = clean_text(f"{style.get('title_prefix', '')}{base_title}{style.get('title_suffix', '')}")
    title = truncate_text(title, limits["title"])

    max_hashtags = int(style.get("max_hashtags") or limits["hashtags"])
    configured_hashtags = style.get("default_hashtags") if isinstance(style.get("default_hashtags"), list) else []
    hashtags = normalize_hashtags(configured_hashtags, max_hashtags=min(max_hashtags, limits["hashtags"]))

    caption = clean_text(source_clip.get("caption") or source_clip.get("hook") or base_title)
    template_key = "x_text_template" if platform == "x" else "caption_template"
    template = str(style.get(template_key) or style.get("description_template") or "{caption}\n\n{hashtags}")
    description = template.format(
        caption=caption,
        hook=clean_text(source_clip.get("hook") or ""),
        title=title,
        hashtags=" ".join(hashtags),
    ).strip()
    description = truncate_text(description, limits["description"])

    normalized_publish_at: str | None = None
    if publish_at:
        parsed = parse_iso_datetime(publish_at)
        if parsed is None:
            issues.append("invalid_publish_at")
        else:
            min_future = datetime.now(UTC) + timedelta(minutes=15)
            if parsed <= min_future:
                issues.append("publish_at_not_safely_future")
            normalized_publish_at = to_utc_iso(parsed)

    return MetadataResult(
        title=title,
        description=description,
        hashtags=hashtags,
        publish_at=normalized_publish_at,
        issues=issues,
    )
