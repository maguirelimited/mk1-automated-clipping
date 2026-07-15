"""Acquisition source type helpers (Funnel Management MK1).

Canonical registry uses singular funnel-level types where possible
(``youtube_channel``, ``youtube_playlist``). Source-input runtime config
uses plural funnel-level types (``youtube_channels``, ``youtube_playlists``).
Per-source entries always use singular types (``youtube_channel``, ``youtube_playlist``).
"""

from __future__ import annotations

ALLOWED_ACQUISITION_SOURCE_TYPES = frozenset(
    {
        "youtube_channel",
        "youtube_channels",
        "youtube_playlist",
        "youtube_playlists",
    }
)

ALLOWED_PER_SOURCE_TYPES = frozenset(
    {
        "youtube_channel",
        "youtube_playlist",
    }
)

CANONICAL_ACQUISITION_SOURCE_TYPES = ("youtube_channel", "youtube_playlist")

ACQUISITION_SOURCE_TYPE_LABELS: dict[str, str] = {
    "youtube_channel": "YouTube channel",
    "youtube_playlist": "YouTube playlist",
}

PER_SOURCE_TYPE_LABELS: dict[str, str] = {
    "youtube_channel": "YouTube channel",
    "youtube_playlist": "YouTube playlist",
}


def validate_acquisition_source_type(value: str, *, field: str) -> str | None:
    """Return an error message when *value* is not an allowed acquisition source type."""
    clean = str(value or "").strip().lower()
    if not clean:
        return f"{field} is required."
    if clean not in ALLOWED_ACQUISITION_SOURCE_TYPES:
        allowed = ", ".join(sorted(CANONICAL_ACQUISITION_SOURCE_TYPES))
        return f"{field} must be one of: {allowed}."
    return None


def validate_per_source_type(value: str, *, field: str) -> str | None:
    """Return an error message when *value* is not an allowed per-source type."""
    clean = str(value or "").strip().lower()
    if not clean:
        return f"{field} is required."
    if clean not in ALLOWED_PER_SOURCE_TYPES:
        allowed = ", ".join(sorted(ALLOWED_PER_SOURCE_TYPES))
        return f"{field} must be one of: {allowed}."
    return None


def default_per_source_type(acquisition_source_type: str) -> str:
    """Infer per-source type from funnel-level acquisition source type."""
    clean = str(acquisition_source_type or "").strip().lower()
    if clean in {"youtube_playlist", "youtube_playlists"}:
        return "youtube_playlist"
    return "youtube_channel"


def is_playlist_acquisition_type(source_type: str) -> bool:
    return str(source_type or "").strip().lower() in {"youtube_playlist", "youtube_playlists"}


def normalize_runtime_acquisition_source_type(canonical_type: str) -> str:
    """Map canonical acquisition source type to source-input runtime shape."""
    clean = str(canonical_type or "").strip().lower()
    if clean in {"youtube_channel", "youtube_channels"}:
        return "youtube_channels"
    if clean in {"youtube_playlist", "youtube_playlists"}:
        return "youtube_playlists"
    return clean


def denormalize_canonical_acquisition_source_type(runtime_type: str) -> str:
    """Map source-input runtime funnel-level type back to canonical singular form."""
    clean = str(runtime_type or "").strip().lower()
    if clean == "youtube_channels":
        return "youtube_channel"
    if clean == "youtube_playlists":
        return "youtube_playlist"
    return clean


def source_url_placeholder(source_type: str) -> str:
    if is_playlist_acquisition_type(source_type):
        return "https://www.youtube.com/playlist?list=PL..."
    return "https://www.youtube.com/@channel/videos"
