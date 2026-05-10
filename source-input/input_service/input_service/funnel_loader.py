"""Load funnel configurations from ``config/funnels.json``.

A funnel is a small dict describing one approved set of YouTube longform
podcast sources. See ``config/funnels.json`` for the canonical shape.
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

SUPPORTED_SOURCE_TYPES = {"youtube_channels"}


class FunnelError(Exception):
    """Base class for funnel-loading problems."""


class FunnelNotFoundError(FunnelError):
    pass


class FunnelInactiveError(FunnelError):
    pass


class FunnelInvalidError(FunnelError):
    pass


@dataclass(frozen=True)
class Funnel:
    funnel_id: str
    angle: str
    source_type: str
    sources: tuple[str, ...]
    min_duration_minutes: int
    max_duration_minutes: int
    active: bool

    @property
    def min_duration_seconds(self) -> int:
        return int(self.min_duration_minutes) * 60

    @property
    def max_duration_seconds(self) -> int:
        return int(self.max_duration_minutes) * 60


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


def _coerce_funnel(data: dict[str, Any]) -> Funnel:
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        raise FunnelInvalidError(
            f"Funnel {data.get('funnel_id', '?')!r} missing fields: {missing}"
        )

    sources = data["sources"]
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise FunnelInvalidError(
            f"Funnel {data['funnel_id']!r} 'sources' must be a list of strings."
        )

    if data["source_type"] not in SUPPORTED_SOURCE_TYPES:
        raise FunnelInvalidError(
            f"Funnel {data['funnel_id']!r} unsupported source_type "
            f"{data['source_type']!r}. Mk1 supports: {sorted(SUPPORTED_SOURCE_TYPES)}."
        )

    try:
        min_min = int(data["min_duration_minutes"])
        max_min = int(data["max_duration_minutes"])
    except (TypeError, ValueError) as exc:
        raise FunnelInvalidError(
            f"Funnel {data['funnel_id']!r} duration fields must be integers."
        ) from exc

    if min_min < 0 or max_min <= 0 or max_min < min_min:
        raise FunnelInvalidError(
            f"Funnel {data['funnel_id']!r} has an invalid duration range "
            f"({min_min}..{max_min} minutes)."
        )

    return Funnel(
        funnel_id=str(data["funnel_id"]),
        angle=str(data["angle"]),
        source_type=str(data["source_type"]),
        sources=tuple(s.strip() for s in sources if s and s.strip()),
        min_duration_minutes=min_min,
        max_duration_minutes=max_min,
        active=bool(data["active"]),
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
