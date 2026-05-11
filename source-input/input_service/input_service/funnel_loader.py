"""Load reusable funnel configurations from ``config/funnels.json``.

The config file is the operational catalogue for input acquisition. A funnel
defines source/channel behaviour plus downstream posting/analytics metadata so
n8n can orchestrate rather than own business rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths


REQUIRED_FIELDS = (
    "funnel_id",
    "angle",
    "source_type",
    "sources",
    "min_duration_minutes",
    "max_duration_minutes",
    "active",
)

SUPPORTED_SOURCE_TYPES = {
    "youtube_channel",
    "youtube_channels",
    "youtube_playlist",
    "youtube_playlists",
    "yt_dlp_collection",
}

DEFAULT_TITLE_BLOCKLIST = (
    "shorts",
    "short",
    "clip",
    "clips",
    "highlight",
    "highlights",
    "trailer",
    "teaser",
    "preview",
    "compilation",
)


class FunnelError(Exception):
    """Base class for funnel-loading problems."""


class FunnelNotFoundError(FunnelError):
    pass


class FunnelInactiveError(FunnelError):
    pass


class FunnelInvalidError(FunnelError):
    pass


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    url: str
    source_type: str
    label: str | None = None
    active: bool = True
    max_videos_per_source: int | None = None
    hydrate_missing_duration: bool = True
    title_blocklist: tuple[str, ...] = ()
    title_allowlist: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "url": self.url,
            "source_type": self.source_type,
            "label": self.label,
            "active": self.active,
            "max_videos_per_source": self.max_videos_per_source,
            "hydrate_missing_duration": self.hydrate_missing_duration,
            "title_blocklist": list(self.title_blocklist),
            "title_allowlist": list(self.title_allowlist),
        }


@dataclass(frozen=True)
class Funnel:
    funnel_id: str
    angle: str
    source_type: str
    source_configs: tuple[SourceDefinition, ...]
    min_duration_minutes: int
    max_duration_minutes: int
    active: bool
    pipeline_profile: str
    max_downloads_per_run: int = 1
    title_blocklist: tuple[str, ...] = DEFAULT_TITLE_BLOCKLIST
    title_allowlist: tuple[str, ...] = ()
    posting_config: dict[str, Any] | None = None
    analytics_config: dict[str, Any] | None = None

    @property
    def min_duration_seconds(self) -> int:
        return int(self.min_duration_minutes) * 60

    @property
    def max_duration_seconds(self) -> int:
        return int(self.max_duration_minutes) * 60

    @property
    def sources(self) -> tuple[str, ...]:
        """Legacy URL tuple retained for older call sites."""
        return tuple(src.url for src in self.source_configs if src.active)

    def as_dict(self) -> dict[str, Any]:
        return {
            "funnel_id": self.funnel_id,
            "angle": self.angle,
            "source_type": self.source_type,
            "sources": [src.as_dict() for src in self.source_configs],
            "min_duration_minutes": self.min_duration_minutes,
            "max_duration_minutes": self.max_duration_minutes,
            "active": self.active,
            "pipeline_profile": self.pipeline_profile,
            "max_downloads_per_run": self.max_downloads_per_run,
            "title_blocklist": list(self.title_blocklist),
            "title_allowlist": list(self.title_allowlist),
            "posting_config": self.posting_config or {},
            "analytics_config": self.analytics_config or {},
        }


def _read_funnels_file(funnels_file: Path | None = None) -> list[dict[str, Any]]:
    path = funnels_file or paths.FUNNELS_FILE
    if not path.exists():
        raise FunnelInvalidError(f"Funnels config file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FunnelInvalidError(f"Invalid JSON in funnels config {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise FunnelInvalidError(
            f"Funnels config must be a JSON list of funnel objects (got {type(raw).__name__})."
        )
    return raw


def _string_tuple(raw: Any, *, field: str, funnel_id: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        raise FunnelInvalidError(
            f"Funnel {funnel_id!r} field {field!r} must be a list of strings."
        )
    return tuple(s.strip() for s in raw if s and s.strip())


def _source_type(raw: Any, *, fallback: str, funnel_id: str) -> str:
    st = str(raw or fallback).strip()
    if st not in SUPPORTED_SOURCE_TYPES:
        raise FunnelInvalidError(
            f"Funnel {funnel_id!r} unsupported source_type {st!r}. "
            f"Supported source types: {sorted(SUPPORTED_SOURCE_TYPES)}."
        )
    return st


def _source_id_from_url(url: str, index: int) -> str:
    cleaned = url.rstrip("/").split("/")[-1] or f"source_{index:02d}"
    return "".join(ch if ch.isalnum() or ch in ("-", "_", "@") else "_" for ch in cleaned)


def _coerce_source(
    raw: Any,
    *,
    funnel_id: str,
    parent_source_type: str,
    index: int,
) -> SourceDefinition:
    if isinstance(raw, str):
        url = raw.strip()
        if not url:
            raise FunnelInvalidError(f"Funnel {funnel_id!r} contains an empty source URL.")
        st = _source_type(parent_source_type, fallback=parent_source_type, funnel_id=funnel_id)
        return SourceDefinition(
            source_id=_source_id_from_url(url, index),
            url=url,
            source_type=st,
        )

    if not isinstance(raw, dict):
        raise FunnelInvalidError(
            f"Funnel {funnel_id!r} source #{index} must be a URL string or object."
        )

    url_raw = raw.get("url") or raw.get("source_url")
    if not isinstance(url_raw, str) or not url_raw.strip():
        raise FunnelInvalidError(
            f"Funnel {funnel_id!r} source #{index} requires non-empty 'url'."
        )
    url = url_raw.strip()
    st = _source_type(raw.get("source_type"), fallback=parent_source_type, funnel_id=funnel_id)

    max_items_raw = raw.get("max_videos_per_source")
    max_items: int | None = None
    if max_items_raw is not None:
        try:
            max_items = int(max_items_raw)
        except (TypeError, ValueError) as exc:
            raise FunnelInvalidError(
                f"Funnel {funnel_id!r} source #{index} max_videos_per_source must be integer."
            ) from exc
        if max_items <= 0:
            raise FunnelInvalidError(
                f"Funnel {funnel_id!r} source #{index} max_videos_per_source must be > 0."
            )

    source_id_raw = raw.get("source_id")
    source_id = (
        str(source_id_raw).strip()
        if isinstance(source_id_raw, str) and source_id_raw.strip()
        else _source_id_from_url(url, index)
    )

    return SourceDefinition(
        source_id=source_id,
        url=url,
        source_type=st,
        label=str(raw.get("label")).strip() if raw.get("label") else None,
        active=bool(raw.get("active", True)),
        max_videos_per_source=max_items,
        hydrate_missing_duration=bool(raw.get("hydrate_missing_duration", True)),
        title_blocklist=_string_tuple(
            raw.get("title_blocklist"), field="source.title_blocklist", funnel_id=funnel_id
        ),
        title_allowlist=_string_tuple(
            raw.get("title_allowlist"), field="source.title_allowlist", funnel_id=funnel_id
        ),
    )


def _coerce_funnel(data: dict[str, Any]) -> Funnel:
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        raise FunnelInvalidError(
            f"Funnel {data.get('funnel_id', '?')!r} missing fields: {missing}"
        )

    funnel_id = str(data["funnel_id"])
    parent_source_type = _source_type(
        data["source_type"], fallback="youtube_channels", funnel_id=funnel_id
    )

    sources = data["sources"]
    if not isinstance(sources, list):
        raise FunnelInvalidError(
            f"Funnel {funnel_id!r} 'sources' must be a list of URL strings or source objects."
        )

    source_configs = tuple(
        _coerce_source(
            raw,
            funnel_id=funnel_id,
            parent_source_type=parent_source_type,
            index=index,
        )
        for index, raw in enumerate(sources, start=1)
    )

    try:
        min_min = int(data["min_duration_minutes"])
        max_min = int(data["max_duration_minutes"])
    except (TypeError, ValueError) as exc:
        raise FunnelInvalidError(
            f"Funnel {funnel_id!r} duration fields must be integers."
        ) from exc

    if min_min < 0 or max_min <= 0 or max_min < min_min:
        raise FunnelInvalidError(
            f"Funnel {funnel_id!r} has an invalid duration range "
            f"({min_min}..{max_min} minutes)."
        )

    try:
        max_downloads = int(data.get("max_downloads_per_run", 1))
    except (TypeError, ValueError) as exc:
        raise FunnelInvalidError(
            f"Funnel {funnel_id!r} max_downloads_per_run must be an integer."
        ) from exc
    if max_downloads != 1:
        raise FunnelInvalidError(
            f"Funnel {funnel_id!r} max_downloads_per_run={max_downloads}; mk0.4 supports exactly 1."
        )

    posting_config = data.get("posting_config") or {}
    analytics_config = data.get("analytics_config") or {}
    if not isinstance(posting_config, dict):
        raise FunnelInvalidError(f"Funnel {funnel_id!r} posting_config must be an object.")
    if not isinstance(analytics_config, dict):
        raise FunnelInvalidError(f"Funnel {funnel_id!r} analytics_config must be an object.")

    pipeline_profile = data.get("pipeline_profile")
    if pipeline_profile is not None:
        pipeline_profile = str(pipeline_profile).strip() or None
    # Match runner semantics: funnel_id is always a valid downstream hint unless a
    # distinct profile/catalog id is explicitly set.
    if pipeline_profile is None:
        pipeline_profile = funnel_id

    title_blocklist = _string_tuple(
        data.get("title_blocklist", list(DEFAULT_TITLE_BLOCKLIST)),
        field="title_blocklist",
        funnel_id=funnel_id,
    )
    title_allowlist = _string_tuple(
        data.get("title_allowlist"),
        field="title_allowlist",
        funnel_id=funnel_id,
    )

    return Funnel(
        funnel_id=funnel_id,
        angle=str(data["angle"]),
        source_type=parent_source_type,
        source_configs=source_configs,
        min_duration_minutes=min_min,
        max_duration_minutes=max_min,
        active=bool(data["active"]),
        pipeline_profile=pipeline_profile,
        max_downloads_per_run=max_downloads,
        title_blocklist=title_blocklist,
        title_allowlist=title_allowlist,
        posting_config=dict(posting_config),
        analytics_config=dict(analytics_config),
    )


def load_funnel(funnel_id: str, *, funnels_file: Path | None = None) -> Funnel:
    """Return the funnel matching ``funnel_id``.

    Raises ``FunnelNotFoundError`` if no such funnel exists, ``FunnelInactiveError``
    if it exists but is inactive, and ``FunnelInvalidError`` if the config is
    malformed or the funnel has no approved sources.
    """
    if not funnel_id or not isinstance(funnel_id, str):
        raise FunnelInvalidError("funnel_id is required and must be a non-empty string.")

    raw_funnels = _read_funnels_file(funnels_file)
    match: dict[str, Any] | None = None
    for entry in raw_funnels:
        if isinstance(entry, dict) and entry.get("funnel_id") == funnel_id:
            match = entry
            break

    if match is None:
        raise FunnelNotFoundError(f"No funnel found with id {funnel_id!r}.")

    funnel = _coerce_funnel(match)

    if not funnel.active:
        raise FunnelInactiveError(f"Funnel {funnel_id!r} is inactive.")

    if not funnel.sources:
        raise FunnelInvalidError(f"Funnel {funnel_id!r} has no approved sources.")

    return funnel


def list_funnels(*, funnels_file: Path | None = None, include_inactive: bool = True) -> list[dict[str, Any]]:
    """Return validated funnel manifests for onboarding/ops UIs."""
    out: list[dict[str, Any]] = []
    for entry in _read_funnels_file(funnels_file):
        if not isinstance(entry, dict):
            raise FunnelInvalidError("Every funnels.json entry must be an object.")
        funnel = _coerce_funnel(entry)
        if include_inactive or funnel.active:
            out.append(funnel.as_dict())
    return out
