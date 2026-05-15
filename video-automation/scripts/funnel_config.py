"""Content funnel config: one JSON file per repeatable clip operation (video-automation).

Distinct from source-input acquisition ``funnels.json``. See ``config/funnels/<funnel_id>.json``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FUNNELS_SUBDIR = "funnels"
_FILENAME_PREFIX_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_FUNNEL_STEM_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

ALLOWED_PLATFORMS = frozenset(
    {
        "tiktok",
        "instagram_reels",
        "youtube_shorts",
        "x",
    }
)

ALLOWED_DELIVERY_MODES = frozenset({"pull_from_output_endpoint"})
DEFAULT_DELIVERY_MODE = "pull_from_output_endpoint"

_ENV_FUNNEL_CONFIG_DIR = "FUNNEL_CONFIG_DIR"


@dataclass(frozen=True)
class ContentFunnelConfig:
    """Validated operational funnel loaded from disk."""

    funnel_id: str
    funnel_name: str
    platforms: dict[str, bool]
    selection_shard: dict[str, Any]
    filename_prefix: str
    delivery_mode: str
    source_path: str

    def as_policy_sidecar(self) -> dict[str, Any]:
        return {
            "funnel_id": self.funnel_id,
            "funnel_name": self.funnel_name,
            "platforms": dict(self.platforms),
            "output": {
                "filename_prefix": self.filename_prefix,
                "delivery_mode": self.delivery_mode,
            },
        }


def resolved_funnel_config_path(*, pipeline_config_abs: str, funnel_id: str) -> str:
    """Absolute path to ``<funnel_id>.json`` for this funnel id."""
    fid = str(funnel_id).strip()
    alt_dir = os.environ.get(_ENV_FUNNEL_CONFIG_DIR, "").strip()
    if alt_dir:
        base = os.path.abspath(alt_dir)
    else:
        base = os.path.join(os.path.dirname(os.path.abspath(pipeline_config_abs)), _FUNNELS_SUBDIR)
    return os.path.join(base, f"{fid}.json")


def funnel_config_file_exists(*, pipeline_config_abs: str, funnel_id: str) -> bool:
    path = resolved_funnel_config_path(pipeline_config_abs=pipeline_config_abs, funnel_id=funnel_id)
    return os.path.isfile(path)


def sanitize_funnel_config_basename(raw: Any) -> str | None:
    """HTTP ``funnel_config``: basename or ``name.json`` stem only (no directories).

    Returns the stem used for ``config/funnels/<stem>.json`` (1–64 chars, alphanumeric, ``_``, ``-``).
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"funnel_config must be a string; got {type(raw).__name__}")
    if not raw.strip():
        return None
    s = raw.strip()
    if ".." in s or "/" in s or "\\" in s:
        raise ValueError("funnel_config must be a basename only (no paths or '..').")
    base = os.path.basename(s)
    stem = base[:-5] if base.lower().endswith(".json") else base
    stem = stem.strip()
    if not stem or not _FUNNEL_STEM_RE.fullmatch(stem):
        raise ValueError(
            "funnel_config must be 1–64 characters from [a-zA-Z0-9_-] "
            f"(optionally with a .json suffix); got {raw!r}."
        )
    return stem


def _require_str(data: dict[str, Any], key: str, *, ctx: str) -> str:
    raw = data.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{ctx}: {key!r} must be a non-empty string")
    return raw.strip()


def _platforms_from_enabled_list(enabled: list[Any], *, ctx: str) -> dict[str, bool]:
    out: dict[str, bool] = {p: False for p in ALLOWED_PLATFORMS}
    for item in enabled:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{ctx}: platforms.enabled entries must be non-empty strings")
        token = item.strip()
        if token not in ALLOWED_PLATFORMS:
            raise ValueError(
                f"{ctx}: unknown platform {token!r}; allowed: {sorted(ALLOWED_PLATFORMS)}"
            )
        out[token] = True
    return out


def _validate_platforms(raw: Any, *, ctx: str) -> dict[str, bool]:
    if raw is None:
        raise ValueError(f"{ctx}: platforms section is required")
    if not isinstance(raw, dict):
        raise ValueError(f"{ctx}: platforms must be an object")
    enabled = raw.get("enabled")
    if isinstance(enabled, list):
        return _platforms_from_enabled_list(enabled, ctx=ctx)
    out: dict[str, bool] = {p: False for p in ALLOWED_PLATFORMS}
    for key, val in raw.items():
        if key == "enabled":
            continue
        if key not in ALLOWED_PLATFORMS:
            raise ValueError(
                f"{ctx}: unknown platforms key {key!r}; allowed: {sorted(ALLOWED_PLATFORMS)}"
            )
        if not isinstance(val, bool):
            raise ValueError(f"{ctx}: platforms.{key} must be a boolean, got {type(val).__name__}")
        out[key] = val
    return out


def _validate_selection(raw: Any, *, ctx: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{ctx}: selection must be an object when present")
    # Numeric / logical checks mirror pipeline_utils._finalize_selection intent
    if "max_clips" in raw and raw["max_clips"] is not None:
        try:
            mc = int(raw["max_clips"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{ctx}: selection.max_clips must be an integer") from exc
        if mc < 1:
            raise ValueError(f"{ctx}: selection.max_clips must be >= 1")
    for key in ("min_duration_sec", "max_duration_sec", "max_overlap_sec"):
        if key not in raw or raw[key] is None:
            continue
        try:
            float(raw[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{ctx}: selection.{key} must be a number") from exc
    if "min_duration_sec" in raw and raw["min_duration_sec"] is not None:
        if "max_duration_sec" in raw and raw["max_duration_sec"] is not None:
            mn = float(raw["min_duration_sec"])
            mx = float(raw["max_duration_sec"])
            if mn > mx:
                raise ValueError(
                    f"{ctx}: selection.min_duration_sec ({mn}) cannot exceed max_duration_sec ({mx})"
                )
    if "max_overlap_sec" in raw and raw["max_overlap_sec"] is not None:
        if float(raw["max_overlap_sec"]) < 0:
            raise ValueError(f"{ctx}: selection.max_overlap_sec cannot be negative")
    return dict(raw)


def _validate_filename_prefix(raw: Any, *, ctx: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    s = raw.strip()
    if not _FILENAME_PREFIX_RE.fullmatch(s):
        raise ValueError(
            f"{ctx}: output.filename_prefix must match {_FILENAME_PREFIX_RE.pattern} (1–64 chars, "
            "letters, digits, underscore, hyphen only)"
        )
    return s


def _validate_delivery_mode(raw: Any, *, ctx: str) -> str:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_DELIVERY_MODE
    if not isinstance(raw, str):
        raise ValueError(f"{ctx}: output.delivery_mode must be a string")
    mode = raw.strip()
    if mode not in ALLOWED_DELIVERY_MODES:
        raise ValueError(
            f"{ctx}: output.delivery_mode {mode!r} is not supported; allowed: {sorted(ALLOWED_DELIVERY_MODES)}"
        )
    return mode


def parse_content_funnel_dict(data: dict[str, Any], *, source_path: str, expected_funnel_id: str) -> ContentFunnelConfig:
    """Validate a funnel object parsed from JSON. ``expected_funnel_id`` is the basename key (must match file)."""
    ctx = f"funnel config {source_path!r}"
    if "schema_version" in data and data["schema_version"] is not None:
        try:
            schema_version = int(data["schema_version"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{ctx}: schema_version must be an integer") from exc
        if schema_version < 1:
            raise ValueError(f"{ctx}: schema_version must be >= 1")

    funnel_id = _require_str(data, "funnel_id", ctx=ctx)
    if funnel_id != expected_funnel_id:
        raise ValueError(
            f"{ctx}: funnel_id {funnel_id!r} must match config file name / request id {expected_funnel_id!r}"
        )
    funnel_name = _require_str(data, "funnel_name", ctx=ctx)
    platforms = _validate_platforms(data.get("platforms"), ctx=ctx)
    selection_raw = _validate_selection(data.get("selection"), ctx=ctx)
    out = data.get("output")
    if out is None:
        out = {}
    if not isinstance(out, dict):
        raise ValueError(f"{ctx}: output must be an object when present")
    filename_prefix = _validate_filename_prefix(out.get("filename_prefix"), ctx=ctx)
    delivery_mode = _validate_delivery_mode(out.get("delivery_mode"), ctx=ctx)

    return ContentFunnelConfig(
        funnel_id=funnel_id,
        funnel_name=funnel_name,
        platforms=platforms,
        selection_shard=selection_raw,
        filename_prefix=filename_prefix,
        delivery_mode=delivery_mode,
        source_path=source_path,
    )


def load_content_funnel_config(*, pipeline_config_abs: str, funnel_id: str) -> ContentFunnelConfig:
    """Load and validate ``config/funnels/<funnel_id>.json``. Raises ``ValueError`` / ``OSError`` on failure."""
    fid = str(funnel_id).strip()
    if not fid:
        raise ValueError("funnel_id is required to load a content funnel config")
    path = resolved_funnel_config_path(pipeline_config_abs=pipeline_config_abs, funnel_id=fid)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in funnel config {path!r}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Funnel config {path!r} must be a JSON object")
    return parse_content_funnel_dict(raw, source_path=os.path.abspath(path), expected_funnel_id=fid)
