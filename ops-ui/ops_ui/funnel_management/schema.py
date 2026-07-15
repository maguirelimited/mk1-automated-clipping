"""Canonical funnel schema (Funnel Management MK1, schema_version 1).

Persistent funnel configuration only. Readiness, operations, runtime state,
prompt text, OAuth, and analytics are intentionally excluded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

SCHEMA_VERSION = 1

ALLOWED_ENVIRONMENTS = frozenset({"dev", "prod"})
ALLOWED_STATUSES = frozenset({"draft", "active", "paused", "testing", "archived", "broken"})
ALLOWED_POSTING_MODES = frozenset({"manual_review", "auto_queue", "disabled"})
ALLOWED_DELIVERY_MODES = frozenset({"pull_from_output_endpoint", "handoff"})
ALLOWED_PROMPT_MANAGED = frozenset({"builtin", "custom"})
DEFAULT_PROMPT_MANAGED = "builtin"
DEFAULT_MAX_VIDEOS_PER_SOURCE = 25
ALLOWED_CONFIG_MANAGER_PRESETS = frozenset({"balanced", "growth", "maximum_quality"})
DEFAULT_CONFIG_MANAGER_PRESET = "balanced"
ALLOWED_PLATFORMS = frozenset(
    {"youtube_shorts", "tiktok", "instagram_reels", "facebook_reels", "x"}
)

TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "identity", "acquisition", "processing", "distribution", "mappings"}
)
IDENTITY_KEYS = frozenset(
    {
        "funnel_id",
        "display_name",
        "description",
        "category",
        "enabled",
        "environment",
        "status",
        "template_source",
        "created_at",
        "updated_at",
        "operator_note",
    }
)
ACQUISITION_KEYS = frozenset(
    {
        "source_type",
        "sources",
        "min_duration_minutes",
        "max_duration_minutes",
        "max_downloads_per_run",
    }
)
SOURCE_KEYS = frozenset(
    {
        "source_id",
        "label",
        "url",
        "source_type",
        "active",
        "max_videos_per_source",
        "hydrate_missing_duration",
        "title_allowlist",
        "title_blocklist",
    }
)
PROCESSING_KEYS = frozenset({"pipeline_profile", "ai_rules", "selection", "output", "platforms"})
AI_RULES_KEYS = frozenset({"ai_rule_profile", "prompt_managed", "prompt_text"})
SELECTION_KEYS = frozenset(
    {
        "max_clips",
        "min_clip_duration_sec",
        "max_clip_duration_sec",
        "max_overlap_sec",
    }
)
OUTPUT_KEYS = frozenset({"filename_prefix", "delivery_mode"})
DISTRIBUTION_KEYS = frozenset(
    {"posting_enabled", "posting_mode", "target_platforms", "channel_routes"}
)
CHANNEL_ROUTE_KEYS = frozenset({"channel_id", "platform", "enabled"})
MAPPINGS_KEYS = frozenset({"config_manager_funnel_id", "config_manager_preset_id"})

_FUNNEL_ID_RE = re.compile(r"^[a-z0-9_]+$")
_FILENAME_PREFIX_RE = re.compile(r"^[a-z0-9_-]+$")
_ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


from .acquisition_sources import (
    validate_acquisition_source_type,
    validate_per_source_type,
)


class CanonicalFunnelSchemaError(ValueError):
    """Raised when canonical funnel data fails schema validation."""


@dataclass(frozen=True)
class Identity:
    funnel_id: str
    display_name: str
    enabled: bool
    environment: str
    status: str
    created_at: str
    updated_at: str
    description: str | None = None
    category: str | None = None
    template_source: str | None = None
    operator_note: str | None = None


@dataclass(frozen=True)
class AcquisitionSource:
    source_id: str
    label: str
    url: str
    source_type: str
    active: bool
    max_videos_per_source: int
    hydrate_missing_duration: bool = True
    title_allowlist: tuple[str, ...] = ()
    title_blocklist: tuple[str, ...] = ()


@dataclass(frozen=True)
class Acquisition:
    source_type: str
    sources: tuple[AcquisitionSource, ...]
    min_duration_minutes: int
    max_duration_minutes: int
    max_downloads_per_run: int


@dataclass(frozen=True)
class AiRules:
    ai_rule_profile: str
    prompt_managed: str = DEFAULT_PROMPT_MANAGED
    prompt_text: str | None = None


@dataclass(frozen=True)
class Selection:
    max_clips: int
    min_clip_duration_sec: int
    max_clip_duration_sec: int
    max_overlap_sec: int


@dataclass(frozen=True)
class Output:
    filename_prefix: str
    delivery_mode: str


@dataclass(frozen=True)
class Processing:
    pipeline_profile: str
    ai_rules: AiRules
    selection: Selection
    output: Output
    platforms: dict[str, bool]


@dataclass(frozen=True)
class ChannelRoute:
    channel_id: str
    platform: str
    enabled: bool


@dataclass(frozen=True)
class Distribution:
    posting_enabled: bool
    posting_mode: str
    target_platforms: tuple[str, ...]
    channel_routes: tuple[ChannelRoute, ...]


@dataclass(frozen=True)
class Mappings:
    config_manager_funnel_id: str | None = None
    config_manager_preset_id: str = DEFAULT_CONFIG_MANAGER_PRESET


@dataclass(frozen=True)
class CanonicalFunnel:
    schema_version: int
    identity: Identity
    acquisition: Acquisition
    processing: Processing
    distribution: Distribution
    mappings: Mappings


def load_canonical_funnel(data: dict[str, Any]) -> CanonicalFunnel:
    """Parse and validate a plain dict into a CanonicalFunnel."""
    if not isinstance(data, dict):
        raise CanonicalFunnelSchemaError("Canonical funnel must be an object.")

    _reject_unknown_keys(data, TOP_LEVEL_KEYS, "top level")

    schema_version = _require_schema_version(data.get("schema_version"))
    identity_data = _require_dict(data.get("identity"), "identity")
    identity = _parse_identity(identity_data)

    acquisition = _parse_acquisition(_require_dict(data.get("acquisition"), "acquisition"))
    processing = _parse_processing(
        _require_dict(data.get("processing"), "processing"),
        default_pipeline_profile=identity.funnel_id,
    )
    distribution = _parse_distribution(_require_dict(data.get("distribution"), "distribution"))
    mappings = _parse_mappings(_require_dict(data.get("mappings"), "mappings"))

    return CanonicalFunnel(
        schema_version=schema_version,
        identity=identity,
        acquisition=acquisition,
        processing=processing,
        distribution=distribution,
        mappings=mappings,
    )


def dump_canonical_funnel(funnel: CanonicalFunnel) -> dict[str, Any]:
    """Serialise a CanonicalFunnel to a plain dict."""
    return {
        "schema_version": funnel.schema_version,
        "identity": {
            "funnel_id": funnel.identity.funnel_id,
            "display_name": funnel.identity.display_name,
            "description": funnel.identity.description,
            "category": funnel.identity.category,
            "enabled": funnel.identity.enabled,
            "environment": funnel.identity.environment,
            "status": funnel.identity.status,
            "template_source": funnel.identity.template_source,
            "created_at": funnel.identity.created_at,
            "updated_at": funnel.identity.updated_at,
            "operator_note": funnel.identity.operator_note,
        },
        "acquisition": {
            "source_type": funnel.acquisition.source_type,
            "sources": [
                {
                    "source_id": source.source_id,
                    "label": source.label,
                    "url": source.url,
                    "source_type": source.source_type,
                    "active": source.active,
                    "max_videos_per_source": source.max_videos_per_source,
                    "hydrate_missing_duration": source.hydrate_missing_duration,
                    "title_allowlist": list(source.title_allowlist),
                    "title_blocklist": list(source.title_blocklist),
                }
                for source in funnel.acquisition.sources
            ],
            "min_duration_minutes": funnel.acquisition.min_duration_minutes,
            "max_duration_minutes": funnel.acquisition.max_duration_minutes,
            "max_downloads_per_run": funnel.acquisition.max_downloads_per_run,
        },
        "processing": {
            "pipeline_profile": funnel.processing.pipeline_profile,
            "ai_rules": _dump_ai_rules(funnel.processing.ai_rules),
            "selection": {
                "max_clips": funnel.processing.selection.max_clips,
                "min_clip_duration_sec": funnel.processing.selection.min_clip_duration_sec,
                "max_clip_duration_sec": funnel.processing.selection.max_clip_duration_sec,
                "max_overlap_sec": funnel.processing.selection.max_overlap_sec,
            },
            "output": {
                "filename_prefix": funnel.processing.output.filename_prefix,
                "delivery_mode": funnel.processing.output.delivery_mode,
            },
            "platforms": dict(funnel.processing.platforms),
        },
        "distribution": {
            "posting_enabled": funnel.distribution.posting_enabled,
            "posting_mode": funnel.distribution.posting_mode,
            "target_platforms": list(funnel.distribution.target_platforms),
            "channel_routes": [
                {
                    "channel_id": route.channel_id,
                    "platform": route.platform,
                    "enabled": route.enabled,
                }
                for route in funnel.distribution.channel_routes
            ],
        },
        "mappings": {
            "config_manager_funnel_id": funnel.mappings.config_manager_funnel_id,
            "config_manager_preset_id": funnel.mappings.config_manager_preset_id,
        },
    }


def _dump_ai_rules(ai_rules: AiRules) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ai_rule_profile": ai_rules.ai_rule_profile,
        "prompt_managed": ai_rules.prompt_managed,
    }
    if ai_rules.prompt_managed == "custom" and ai_rules.prompt_text:
        payload["prompt_text"] = ai_rules.prompt_text
    return payload


def _reject_unknown_keys(data: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(data.keys()) - allowed)
    if unknown:
        raise CanonicalFunnelSchemaError(
            f"Unknown field(s) at {label}: {', '.join(unknown)}"
        )


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CanonicalFunnelSchemaError(f"Missing or invalid section: {label}")
    return value


def _require_schema_version(value: Any) -> int:
    if value is None:
        raise CanonicalFunnelSchemaError("Missing required field: schema_version")
    if not isinstance(value, int) or isinstance(value, bool):
        raise CanonicalFunnelSchemaError("schema_version must be an integer")
    if value != SCHEMA_VERSION:
        raise CanonicalFunnelSchemaError(
            f"Unsupported schema_version {value!r}; only version {SCHEMA_VERSION} is accepted"
        )
    return value


def _require_non_empty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CanonicalFunnelSchemaError(f"{field_name} must be a non-empty string")
    return value.strip()


def _validated_acquisition_source_type(value: Any, field_name: str) -> str:
    clean = _require_non_empty_str(value, field_name).lower()
    error = validate_acquisition_source_type(clean, field=field_name)
    if error:
        raise CanonicalFunnelSchemaError(error)
    return clean


def _validated_per_source_type(value: Any, field_name: str) -> str:
    clean = _require_non_empty_str(value, field_name).lower()
    error = validate_per_source_type(clean, field=field_name)
    if error:
        raise CanonicalFunnelSchemaError(error)
    return clean


def _optional_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CanonicalFunnelSchemaError(f"{field_name} must be a string or null")
    text = value.strip()
    return text or None


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise CanonicalFunnelSchemaError(f"{field_name} must be a boolean")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CanonicalFunnelSchemaError(f"{field_name} must be a positive integer")
    if value <= 0:
        raise CanonicalFunnelSchemaError(f"{field_name} must be a positive integer")
    return value


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CanonicalFunnelSchemaError(f"{field_name} must be a non-negative integer")
    if value < 0:
        raise CanonicalFunnelSchemaError(f"{field_name} must be a non-negative integer")
    return value


def _require_iso_timestamp(value: Any, field_name: str) -> str:
    text = _require_non_empty_str(value, field_name)
    if not _ISO8601_RE.match(text):
        raise CanonicalFunnelSchemaError(
            f"{field_name} must be an ISO-8601 timestamp (e.g. 2026-07-04T00:00:00Z)"
        )
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CanonicalFunnelSchemaError(f"{field_name} is not a valid ISO-8601 timestamp") from exc
    return text


def _validate_funnel_id(value: str) -> str:
    funnel_id = _require_non_empty_str(value, "identity.funnel_id")
    if len(funnel_id) > 128:
        raise CanonicalFunnelSchemaError("identity.funnel_id is too long")
    if not _FUNNEL_ID_RE.match(funnel_id):
        raise CanonicalFunnelSchemaError(
            "identity.funnel_id must contain only lowercase letters, numbers, and underscores"
        )
    return funnel_id


def _validate_filename_prefix(value: str) -> str:
    prefix = _require_non_empty_str(value, "processing.output.filename_prefix")
    if len(prefix) > 128:
        raise CanonicalFunnelSchemaError("processing.output.filename_prefix is too long")
    if not _FILENAME_PREFIX_RE.match(prefix):
        raise CanonicalFunnelSchemaError(
            "processing.output.filename_prefix must contain only letters, numbers, underscores, and hyphens"
        )
    return prefix


def _validate_url(value: str, field_name: str) -> str:
    url = _require_non_empty_str(value, field_name)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CanonicalFunnelSchemaError(f"{field_name} must be a valid http or https URL")
    if " " in url:
        raise CanonicalFunnelSchemaError(f"{field_name} must not contain spaces")
    return url


def _validate_platform_name(value: str, field_name: str) -> str:
    platform = _require_non_empty_str(value, field_name)
    if platform not in ALLOWED_PLATFORMS:
        raise CanonicalFunnelSchemaError(
            f"{field_name} {platform!r} is not supported; "
            f"allowed platforms: {', '.join(sorted(ALLOWED_PLATFORMS))}"
        )
    return platform


def _parse_string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise CanonicalFunnelSchemaError(f"{field_name} must be a list")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise CanonicalFunnelSchemaError(f"{field_name}[{index}] must be a non-empty string")
        items.append(item.strip())
    return tuple(items)


def _parse_identity(data: dict[str, Any]) -> Identity:
    _reject_unknown_keys(data, IDENTITY_KEYS, "identity")
    for required in ("funnel_id", "display_name", "enabled", "environment", "status", "created_at", "updated_at"):
        if required not in data:
            raise CanonicalFunnelSchemaError(f"Missing required field: identity.{required}")

    environment = _require_non_empty_str(data["environment"], "identity.environment").lower()
    if environment not in ALLOWED_ENVIRONMENTS:
        raise CanonicalFunnelSchemaError(
            f"identity.environment {environment!r} is invalid; allowed: {', '.join(sorted(ALLOWED_ENVIRONMENTS))}"
        )

    status = _require_non_empty_str(data["status"], "identity.status").lower()
    if status not in ALLOWED_STATUSES:
        raise CanonicalFunnelSchemaError(
            f"identity.status {status!r} is invalid; allowed: {', '.join(sorted(ALLOWED_STATUSES))}"
        )

    return Identity(
        funnel_id=_validate_funnel_id(data["funnel_id"]),
        display_name=_require_non_empty_str(data["display_name"], "identity.display_name"),
        enabled=_require_bool(data["enabled"], "identity.enabled"),
        environment=environment,
        status=status,
        created_at=_require_iso_timestamp(data["created_at"], "identity.created_at"),
        updated_at=_require_iso_timestamp(data["updated_at"], "identity.updated_at"),
        description=_optional_str(data.get("description"), "identity.description"),
        category=_optional_str(data.get("category"), "identity.category"),
        template_source=_optional_str(data.get("template_source"), "identity.template_source"),
        operator_note=_optional_str(data.get("operator_note"), "identity.operator_note"),
    )


def _parse_acquisition(data: dict[str, Any]) -> Acquisition:
    _reject_unknown_keys(data, ACQUISITION_KEYS, "acquisition")
    for required in (
        "source_type",
        "sources",
        "min_duration_minutes",
        "max_duration_minutes",
        "max_downloads_per_run",
    ):
        if required not in data:
            raise CanonicalFunnelSchemaError(f"Missing required field: acquisition.{required}")

    sources_raw = data["sources"]
    if not isinstance(sources_raw, list):
        raise CanonicalFunnelSchemaError("acquisition.sources must be a list")

    sources: list[AcquisitionSource] = []
    for index, item in enumerate(sources_raw):
        if not isinstance(item, dict):
            raise CanonicalFunnelSchemaError(f"acquisition.sources[{index}] must be an object")
        _reject_unknown_keys(item, SOURCE_KEYS, f"acquisition.sources[{index}]")
        for required in ("source_id", "label", "url", "source_type", "active", "max_videos_per_source"):
            if required not in item:
                raise CanonicalFunnelSchemaError(
                    f"Missing required field: acquisition.sources[{index}].{required}"
                )
        sources.append(
            AcquisitionSource(
                source_id=_require_non_empty_str(item["source_id"], f"acquisition.sources[{index}].source_id"),
                label=_require_non_empty_str(item["label"], f"acquisition.sources[{index}].label"),
                url=_validate_url(item["url"], f"acquisition.sources[{index}].url"),
                source_type=_validated_per_source_type(
                    item["source_type"], f"acquisition.sources[{index}].source_type"
                ),
                active=_require_bool(item["active"], f"acquisition.sources[{index}].active"),
                max_videos_per_source=_require_positive_int(
                    item["max_videos_per_source"],
                    f"acquisition.sources[{index}].max_videos_per_source",
                ),
                hydrate_missing_duration=(
                    True
                    if "hydrate_missing_duration" not in item
                    else _require_bool(
                        item["hydrate_missing_duration"],
                        f"acquisition.sources[{index}].hydrate_missing_duration",
                    )
                ),
                title_allowlist=_parse_string_list(
                    [] if "title_allowlist" not in item else item["title_allowlist"],
                    f"acquisition.sources[{index}].title_allowlist",
                ),
                title_blocklist=_parse_string_list(
                    [] if "title_blocklist" not in item else item["title_blocklist"],
                    f"acquisition.sources[{index}].title_blocklist",
                ),
            )
        )

    min_duration = _require_positive_int(data["min_duration_minutes"], "acquisition.min_duration_minutes")
    max_duration = _require_positive_int(data["max_duration_minutes"], "acquisition.max_duration_minutes")
    if min_duration >= max_duration:
        raise CanonicalFunnelSchemaError(
            "acquisition.min_duration_minutes must be less than acquisition.max_duration_minutes"
        )

    return Acquisition(
        source_type=_validated_acquisition_source_type(data["source_type"], "acquisition.source_type"),
        sources=tuple(sources),
        min_duration_minutes=min_duration,
        max_duration_minutes=max_duration,
        max_downloads_per_run=_require_positive_int(
            data["max_downloads_per_run"], "acquisition.max_downloads_per_run"
        ),
    )


def _parse_processing(data: dict[str, Any], *, default_pipeline_profile: str) -> Processing:
    _reject_unknown_keys(data, PROCESSING_KEYS, "processing")
    for required in ("ai_rules", "selection", "output", "platforms"):
        if required not in data:
            raise CanonicalFunnelSchemaError(f"Missing required field: processing.{required}")

    pipeline_profile = (
        _require_non_empty_str(data["pipeline_profile"], "processing.pipeline_profile")
        if "pipeline_profile" in data
        else default_pipeline_profile
    )

    ai_rules_data = _require_dict(data["ai_rules"], "processing.ai_rules")
    ai_rules = _parse_ai_rules(ai_rules_data)

    selection_data = _require_dict(data["selection"], "processing.selection")
    _reject_unknown_keys(selection_data, SELECTION_KEYS, "processing.selection")
    for required in SELECTION_KEYS:
        if required not in selection_data:
            raise CanonicalFunnelSchemaError(f"Missing required field: processing.selection.{required}")
    min_clip = _require_positive_int(
        selection_data["min_clip_duration_sec"], "processing.selection.min_clip_duration_sec"
    )
    max_clip = _require_positive_int(
        selection_data["max_clip_duration_sec"], "processing.selection.max_clip_duration_sec"
    )
    if min_clip >= max_clip:
        raise CanonicalFunnelSchemaError(
            "processing.selection.min_clip_duration_sec must be less than max_clip_duration_sec"
        )
    selection = Selection(
        max_clips=_require_positive_int(selection_data["max_clips"], "processing.selection.max_clips"),
        min_clip_duration_sec=min_clip,
        max_clip_duration_sec=max_clip,
        max_overlap_sec=_require_non_negative_int(
            selection_data["max_overlap_sec"], "processing.selection.max_overlap_sec"
        ),
    )

    output_data = _require_dict(data["output"], "processing.output")
    _reject_unknown_keys(output_data, OUTPUT_KEYS, "processing.output")
    for required in OUTPUT_KEYS:
        if required not in output_data:
            raise CanonicalFunnelSchemaError(f"Missing required field: processing.output.{required}")
    delivery_mode = _require_non_empty_str(output_data["delivery_mode"], "processing.output.delivery_mode")
    if delivery_mode not in ALLOWED_DELIVERY_MODES:
        raise CanonicalFunnelSchemaError(
            f"processing.output.delivery_mode {delivery_mode!r} is invalid; "
            f"allowed: {', '.join(sorted(ALLOWED_DELIVERY_MODES))}"
        )
    output = Output(
        filename_prefix=_validate_filename_prefix(output_data["filename_prefix"]),
        delivery_mode=delivery_mode,
    )

    platforms_data = data["platforms"]
    if not isinstance(platforms_data, dict):
        raise CanonicalFunnelSchemaError("processing.platforms must be an object")
    _reject_unknown_keys(platforms_data, ALLOWED_PLATFORMS, "processing.platforms")
    platforms: dict[str, bool] = {}
    for key, value in platforms_data.items():
        platforms[key] = _require_bool(value, f"processing.platforms.{key}")

    return Processing(
        pipeline_profile=pipeline_profile,
        ai_rules=ai_rules,
        selection=selection,
        output=output,
        platforms=platforms,
    )


def _parse_ai_rules(data: dict[str, Any]) -> AiRules:
    _reject_unknown_keys(data, AI_RULES_KEYS, "processing.ai_rules")
    if "ai_rule_profile" not in data:
        raise CanonicalFunnelSchemaError("Missing required field: processing.ai_rules.ai_rule_profile")

    ai_rule_profile = _require_non_empty_str(
        data["ai_rule_profile"], "processing.ai_rules.ai_rule_profile"
    )
    prompt_managed = (
        DEFAULT_PROMPT_MANAGED
        if "prompt_managed" not in data
        else _require_non_empty_str(data["prompt_managed"], "processing.ai_rules.prompt_managed").lower()
    )
    if prompt_managed not in ALLOWED_PROMPT_MANAGED:
        raise CanonicalFunnelSchemaError(
            f"processing.ai_rules.prompt_managed {prompt_managed!r} is invalid; "
            f"allowed: {', '.join(sorted(ALLOWED_PROMPT_MANAGED))}"
        )

    prompt_text = _optional_str(data.get("prompt_text"), "processing.ai_rules.prompt_text")
    if prompt_managed == "custom":
        if not prompt_text:
            raise CanonicalFunnelSchemaError(
                "processing.ai_rules.prompt_text is required when prompt_managed is custom"
            )
    elif prompt_text is not None:
        prompt_text = None

    return AiRules(
        ai_rule_profile=ai_rule_profile,
        prompt_managed=prompt_managed,
        prompt_text=prompt_text,
    )


def _parse_distribution(data: dict[str, Any]) -> Distribution:
    _reject_unknown_keys(data, DISTRIBUTION_KEYS, "distribution")
    for required in DISTRIBUTION_KEYS:
        if required not in data:
            raise CanonicalFunnelSchemaError(f"Missing required field: distribution.{required}")

    posting_mode = _require_non_empty_str(data["posting_mode"], "distribution.posting_mode").lower()
    if posting_mode not in ALLOWED_POSTING_MODES:
        raise CanonicalFunnelSchemaError(
            f"distribution.posting_mode {posting_mode!r} is invalid; "
            f"allowed: {', '.join(sorted(ALLOWED_POSTING_MODES))}"
        )

    target_raw = data["target_platforms"]
    if not isinstance(target_raw, list):
        raise CanonicalFunnelSchemaError("distribution.target_platforms must be a list")
    target_platforms = tuple(
        _validate_platform_name(item, "distribution.target_platforms[]")
        for item in target_raw
    )

    routes_raw = data["channel_routes"]
    if not isinstance(routes_raw, list):
        raise CanonicalFunnelSchemaError("distribution.channel_routes must be a list")
    channel_routes: list[ChannelRoute] = []
    for index, item in enumerate(routes_raw):
        if not isinstance(item, dict):
            raise CanonicalFunnelSchemaError(f"distribution.channel_routes[{index}] must be an object")
        _reject_unknown_keys(item, CHANNEL_ROUTE_KEYS, f"distribution.channel_routes[{index}]")
        for required in CHANNEL_ROUTE_KEYS:
            if required not in item:
                raise CanonicalFunnelSchemaError(
                    f"Missing required field: distribution.channel_routes[{index}].{required}"
                )
        channel_routes.append(
            ChannelRoute(
                channel_id=_require_non_empty_str(
                    item["channel_id"], f"distribution.channel_routes[{index}].channel_id"
                ),
                platform=_validate_platform_name(
                    item["platform"], f"distribution.channel_routes[{index}].platform"
                ),
                enabled=_require_bool(item["enabled"], f"distribution.channel_routes[{index}].enabled"),
            )
        )

    return Distribution(
        posting_enabled=_require_bool(data["posting_enabled"], "distribution.posting_enabled"),
        posting_mode=posting_mode,
        target_platforms=target_platforms,
        channel_routes=tuple(channel_routes),
    )


def _parse_mappings(data: dict[str, Any]) -> Mappings:
    _reject_unknown_keys(data, MAPPINGS_KEYS, "mappings")

    preset = (
        DEFAULT_CONFIG_MANAGER_PRESET
        if "config_manager_preset_id" not in data
        else _require_non_empty_str(
            data["config_manager_preset_id"], "mappings.config_manager_preset_id"
        ).lower()
    )
    if preset not in ALLOWED_CONFIG_MANAGER_PRESETS:
        raise CanonicalFunnelSchemaError(
            f"mappings.config_manager_preset_id {preset!r} is invalid; "
            f"allowed: {', '.join(sorted(ALLOWED_CONFIG_MANAGER_PRESETS))}"
        )

    return Mappings(
        config_manager_funnel_id=_optional_str(
            data.get("config_manager_funnel_id"), "mappings.config_manager_funnel_id"
        ),
        config_manager_preset_id=preset,
    )
