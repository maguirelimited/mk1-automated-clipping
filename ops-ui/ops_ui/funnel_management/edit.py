"""Edit-funnel helpers (Funnel Management MK1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping

from .acquisition_sources import (
    denormalize_canonical_acquisition_source_type,
    validate_acquisition_source_type,
    validate_per_source_type,
)
from .registry import FunnelRegistry
from .schema import (
    ALLOWED_DELIVERY_MODES,
    ALLOWED_ENVIRONMENTS,
    ALLOWED_PLATFORMS,
    ALLOWED_POSTING_MODES,
    ALLOWED_STATUSES,
    CanonicalFunnel,
    CanonicalFunnelSchemaError,
    dump_canonical_funnel,
    load_canonical_funnel,
)


class FunnelEditError(ValueError):
    """Raised when edit form data cannot produce a valid canonical funnel."""


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _checkbox_on(form: Mapping[str, Any], name: str, *, default: bool = False) -> bool:
    if name not in form:
        return default
    return str(form.get(name) or "").strip().lower() in {"on", "true", "1", "yes"}


def _parse_title_list(raw: str) -> list[str]:
    if not raw.strip():
        return []
    items: list[str] = []
    for line in raw.replace(",", "\n").split("\n"):
        text = line.strip()
        if text:
            items.append(text)
    return items


def _format_title_list(values: tuple[str, ...]) -> str:
    return "\n".join(values)


def _source_row_from_funnel(source: Any) -> dict[str, str]:
    return {
        "source_id": source.source_id,
        "label": source.label,
        "url": source.url,
        "source_type": source.source_type,
        "active": "on" if source.active else "",
        "max_videos_per_source": str(source.max_videos_per_source),
        "hydrate_missing_duration": "on" if source.hydrate_missing_duration else "",
        "title_allowlist": _format_title_list(source.title_allowlist),
        "title_blocklist": _format_title_list(source.title_blocklist),
        "remove": "",
    }


def _route_row_from_funnel(route: Any) -> dict[str, str]:
    return {
        "channel_id": route.channel_id,
        "platform": route.platform,
        "enabled": "on" if route.enabled else "",
        "remove": "",
    }


def _empty_source_row() -> dict[str, str]:
    return {
        "source_id": "",
        "label": "",
        "url": "",
        "source_type": "",
        "active": "on",
        "max_videos_per_source": "5",
        "hydrate_missing_duration": "on",
        "title_allowlist": "",
        "title_blocklist": "",
        "remove": "",
    }


def _empty_route_row() -> dict[str, str]:
    return {
        "channel_id": "",
        "platform": "youtube_shorts",
        "enabled": "on",
        "remove": "",
    }


def edit_form_from_funnel(funnel: CanonicalFunnel) -> dict[str, Any]:
    """Build edit-form values from an existing canonical funnel."""
    sources = [_source_row_from_funnel(source) for source in funnel.acquisition.sources]
    routes = [_route_row_from_funnel(route) for route in funnel.distribution.channel_routes]
    return {
        "funnel_id": funnel.identity.funnel_id,
        "display_name": funnel.identity.display_name,
        "description": funnel.identity.description or "",
        "category": funnel.identity.category or "",
        "status": funnel.identity.status,
        "enabled": "on" if funnel.identity.enabled else "",
        "environment": funnel.identity.environment,
        "operator_note": funnel.identity.operator_note or "",
        "created_at": funnel.identity.created_at,
        "template_source": funnel.identity.template_source or "",
        "acquisition_source_type": denormalize_canonical_acquisition_source_type(
            funnel.acquisition.source_type
        ),
        "min_duration_minutes": str(funnel.acquisition.min_duration_minutes),
        "max_duration_minutes": str(funnel.acquisition.max_duration_minutes),
        "max_downloads_per_run": str(funnel.acquisition.max_downloads_per_run),
        "sources": sources or [_empty_source_row()],
        "pipeline_profile": funnel.processing.pipeline_profile,
        "ai_rule_profile": funnel.processing.ai_rules.ai_rule_profile,
        "max_clips": str(funnel.processing.selection.max_clips),
        "min_clip_duration_sec": str(funnel.processing.selection.min_clip_duration_sec),
        "max_clip_duration_sec": str(funnel.processing.selection.max_clip_duration_sec),
        "max_overlap_sec": str(funnel.processing.selection.max_overlap_sec),
        "filename_prefix": funnel.processing.output.filename_prefix,
        "delivery_mode": funnel.processing.output.delivery_mode,
        "platforms": {
            platform: "on" if funnel.processing.platforms.get(platform, False) else ""
            for platform in sorted(ALLOWED_PLATFORMS)
        },
        "posting_enabled": "on" if funnel.distribution.posting_enabled else "",
        "posting_mode": funnel.distribution.posting_mode,
        "target_platforms": {
            platform: "on" if platform in funnel.distribution.target_platforms else ""
            for platform in sorted(ALLOWED_PLATFORMS)
        },
        "routes": routes or [_empty_route_row()],
        "config_manager_funnel_id": funnel.mappings.config_manager_funnel_id or "",
        "new_source": _empty_source_row(),
        "new_route": _empty_route_row(),
    }


def _parse_sources(form: Mapping[str, Any], errors: list[str]) -> list[dict[str, Any]]:
    try:
        count = int(str(form.get("source_count") or len(form.get("sources", [])) or "0"))
    except ValueError:
        count = 0
        errors.append("Source count must be a valid integer.")

    sources: list[dict[str, Any]] = []
    for index in range(count):
        if _checkbox_on(form, f"source_{index}_remove"):
            continue

        source_id = str(form.get(f"source_{index}_source_id") or "").strip()
        label = str(form.get(f"source_{index}_label") or "").strip()
        url = str(form.get(f"source_{index}_url") or "").strip()
        source_type = str(form.get(f"source_{index}_source_type") or "").strip()
        if not source_id and not label and not url:
            continue
        if not source_id:
            errors.append(f"Source {index + 1}: source ID is required.")
        if not label:
            errors.append(f"Source {index + 1}: label is required.")
        if not url:
            errors.append(f"Source {index + 1}: URL is required.")
        if not source_type:
            errors.append(f"Source {index + 1}: source type is required.")
        elif (type_error := validate_per_source_type(source_type, field=f"Source {index + 1} type")):
            errors.append(type_error)

        max_videos_raw = str(form.get(f"source_{index}_max_videos_per_source") or "5").strip()
        try:
            max_videos = int(max_videos_raw)
            if max_videos <= 0:
                errors.append(f"Source {index + 1}: max videos per source must be positive.")
        except ValueError:
            errors.append(f"Source {index + 1}: max videos per source must be a positive integer.")
            max_videos = 5

        sources.append(
            {
                "source_id": source_id,
                "label": label,
                "url": url,
                "source_type": source_type,
                "active": _checkbox_on(form, f"source_{index}_active", default=True),
                "max_videos_per_source": max_videos,
                "hydrate_missing_duration": _checkbox_on(
                    form, f"source_{index}_hydrate_missing_duration", default=True
                ),
                "title_allowlist": _parse_title_list(
                    str(form.get(f"source_{index}_title_allowlist") or "")
                ),
                "title_blocklist": _parse_title_list(
                    str(form.get(f"source_{index}_title_blocklist") or "")
                ),
            }
        )

    new_url = str(form.get("new_source_url") or "").strip()
    if new_url:
        new_source_id = str(form.get("new_source_source_id") or "").strip()
        new_label = str(form.get("new_source_label") or "").strip()
        new_source_type = str(form.get("new_source_source_type") or "").strip()
        if not new_source_id:
            errors.append("New source: source ID is required.")
        if not new_label:
            errors.append("New source: label is required.")
        if not new_source_type:
            errors.append("New source: source type is required.")
        elif (type_error := validate_per_source_type(new_source_type, field="New source type")):
            errors.append(type_error)
        max_videos_raw = str(form.get("new_source_max_videos_per_source") or "5").strip()
        try:
            max_videos = int(max_videos_raw)
            if max_videos <= 0:
                errors.append("New source: max videos per source must be positive.")
        except ValueError:
            errors.append("New source: max videos per source must be a positive integer.")
            max_videos = 5
        sources.append(
            {
                "source_id": new_source_id,
                "label": new_label,
                "url": new_url,
                "source_type": new_source_type,
                "active": _checkbox_on(form, "new_source_active", default=True),
                "max_videos_per_source": max_videos,
                "hydrate_missing_duration": _checkbox_on(
                    form, "new_source_hydrate_missing_duration", default=True
                ),
                "title_allowlist": _parse_title_list(str(form.get("new_source_title_allowlist") or "")),
                "title_blocklist": _parse_title_list(str(form.get("new_source_title_blocklist") or "")),
            }
        )

    return sources


def _parse_routes(form: Mapping[str, Any], errors: list[str]) -> list[dict[str, Any]]:
    try:
        count = int(str(form.get("route_count") or "0"))
    except ValueError:
        count = 0
        errors.append("Route count must be a valid integer.")

    routes: list[dict[str, Any]] = []
    for index in range(count):
        if _checkbox_on(form, f"route_{index}_remove"):
            continue

        channel_id = str(form.get(f"route_{index}_channel_id") or "").strip()
        platform = str(form.get(f"route_{index}_platform") or "").strip()
        if not channel_id and not platform:
            continue
        if not channel_id:
            errors.append(f"Route {index + 1}: channel ID is required.")
        if not platform:
            errors.append(f"Route {index + 1}: platform is required.")

        routes.append(
            {
                "channel_id": channel_id,
                "platform": platform,
                "enabled": _checkbox_on(form, f"route_{index}_enabled", default=True),
            }
        )

    new_channel_id = str(form.get("new_route_channel_id") or "").strip()
    new_platform = str(form.get("new_route_platform") or "").strip()
    if new_channel_id or new_platform:
        if not new_channel_id:
            errors.append("New route: channel ID is required.")
        if not new_platform:
            errors.append("New route: platform is required.")
        routes.append(
            {
                "channel_id": new_channel_id,
                "platform": new_platform,
                "enabled": _checkbox_on(form, "new_route_enabled", default=True),
            }
        )

    return routes


def form_values_from_request(form: Mapping[str, Any]) -> dict[str, Any]:
    """Extract submitted form values for re-rendering after validation errors."""
    try:
        source_count = int(str(form.get("source_count") or "0"))
    except ValueError:
        source_count = 0
    sources: list[dict[str, str]] = []
    for index in range(source_count):
        sources.append(
            {
                "source_id": str(form.get(f"source_{index}_source_id") or ""),
                "label": str(form.get(f"source_{index}_label") or ""),
                "url": str(form.get(f"source_{index}_url") or ""),
                "source_type": str(form.get(f"source_{index}_source_type") or ""),
                "active": "on" if _checkbox_on(form, f"source_{index}_active", default=True) else "",
                "max_videos_per_source": str(form.get(f"source_{index}_max_videos_per_source") or "5"),
                "hydrate_missing_duration": (
                    "on"
                    if _checkbox_on(form, f"source_{index}_hydrate_missing_duration", default=True)
                    else ""
                ),
                "title_allowlist": str(form.get(f"source_{index}_title_allowlist") or ""),
                "title_blocklist": str(form.get(f"source_{index}_title_blocklist") or ""),
                "remove": "on" if _checkbox_on(form, f"source_{index}_remove") else "",
            }
        )

    try:
        route_count = int(str(form.get("route_count") or "0"))
    except ValueError:
        route_count = 0
    routes: list[dict[str, str]] = []
    for index in range(route_count):
        routes.append(
            {
                "channel_id": str(form.get(f"route_{index}_channel_id") or ""),
                "platform": str(form.get(f"route_{index}_platform") or ""),
                "enabled": "on" if _checkbox_on(form, f"route_{index}_enabled", default=True) else "",
                "remove": "on" if _checkbox_on(form, f"route_{index}_remove") else "",
            }
        )

    return {
        "funnel_id": str(form.get("funnel_id") or ""),
        "display_name": str(form.get("display_name") or ""),
        "description": str(form.get("description") or ""),
        "category": str(form.get("category") or ""),
        "status": str(form.get("status") or "draft"),
        "enabled": "on" if _checkbox_on(form, "enabled") else "",
        "environment": str(form.get("environment") or "dev"),
        "operator_note": str(form.get("operator_note") or ""),
        "created_at": str(form.get("created_at") or ""),
        "template_source": str(form.get("template_source") or ""),
        "acquisition_source_type": str(form.get("acquisition_source_type") or ""),
        "min_duration_minutes": str(form.get("min_duration_minutes") or ""),
        "max_duration_minutes": str(form.get("max_duration_minutes") or ""),
        "max_downloads_per_run": str(form.get("max_downloads_per_run") or ""),
        "sources": sources or [_empty_source_row()],
        "pipeline_profile": str(form.get("pipeline_profile") or ""),
        "ai_rule_profile": str(form.get("ai_rule_profile") or ""),
        "max_clips": str(form.get("max_clips") or ""),
        "min_clip_duration_sec": str(form.get("min_clip_duration_sec") or ""),
        "max_clip_duration_sec": str(form.get("max_clip_duration_sec") or ""),
        "max_overlap_sec": str(form.get("max_overlap_sec") or ""),
        "filename_prefix": str(form.get("filename_prefix") or ""),
        "delivery_mode": str(form.get("delivery_mode") or "handoff"),
        "platforms": {
            platform: "on" if _checkbox_on(form, f"platform_{platform}") else ""
            for platform in sorted(ALLOWED_PLATFORMS)
        },
        "posting_enabled": "on" if _checkbox_on(form, "posting_enabled") else "",
        "posting_mode": str(form.get("posting_mode") or "disabled"),
        "target_platforms": {
            platform: "on" if _checkbox_on(form, f"target_platform_{platform}") else ""
            for platform in sorted(ALLOWED_PLATFORMS)
        },
        "routes": routes or [_empty_route_row()],
        "config_manager_funnel_id": str(form.get("config_manager_funnel_id") or ""),
        "new_source": {
            "source_id": str(form.get("new_source_source_id") or ""),
            "label": str(form.get("new_source_label") or ""),
            "url": str(form.get("new_source_url") or ""),
            "source_type": str(form.get("new_source_source_type") or ""),
            "active": "on" if _checkbox_on(form, "new_source_active", default=True) else "",
            "max_videos_per_source": str(form.get("new_source_max_videos_per_source") or "5"),
            "hydrate_missing_duration": (
                "on" if _checkbox_on(form, "new_source_hydrate_missing_duration", default=True) else ""
            ),
            "title_allowlist": str(form.get("new_source_title_allowlist") or ""),
            "title_blocklist": str(form.get("new_source_title_blocklist") or ""),
            "remove": "",
        },
        "new_route": {
            "channel_id": str(form.get("new_route_channel_id") or ""),
            "platform": str(form.get("new_route_platform") or "youtube_shorts"),
            "enabled": "on" if _checkbox_on(form, "new_route_enabled", default=True) else "",
            "remove": "",
        },
    }


def update_funnel_from_form(
    existing: CanonicalFunnel,
    form: Mapping[str, Any],
) -> tuple[CanonicalFunnel | None, list[str]]:
    """Apply allowed form edits to a copy of an existing funnel and re-validate."""
    errors: list[str] = []
    payload = dump_canonical_funnel(existing)

    display_name = str(form.get("display_name") or "").strip()
    if not display_name:
        errors.append("Display name is required.")

    environment = str(form.get("environment") or "dev").strip().lower() or "dev"
    if environment not in ALLOWED_ENVIRONMENTS:
        errors.append("Environment must be dev or prod.")

    status = str(form.get("status") or "draft").strip().lower() or "draft"
    if status not in ALLOWED_STATUSES:
        errors.append(
            f"Status must be one of: {', '.join(sorted(ALLOWED_STATUSES))}."
        )

    posting_mode = str(form.get("posting_mode") or "disabled").strip().lower() or "disabled"
    if posting_mode not in ALLOWED_POSTING_MODES:
        errors.append(
            f"Posting mode must be one of: {', '.join(sorted(ALLOWED_POSTING_MODES))}."
        )

    delivery_mode = str(form.get("delivery_mode") or "handoff").strip()
    if delivery_mode not in ALLOWED_DELIVERY_MODES:
        errors.append(
            f"Delivery mode must be one of: {', '.join(sorted(ALLOWED_DELIVERY_MODES))}."
        )

    submitted_funnel_id = str(form.get("funnel_id") or "").strip()
    original_funnel_id = existing.identity.funnel_id
    if submitted_funnel_id and submitted_funnel_id != original_funnel_id:
        errors.append("Funnel ID cannot be changed.")

    pipeline_profile = str(form.get("pipeline_profile") or "").strip()
    if not pipeline_profile:
        pipeline_profile = original_funnel_id

    ai_rule_profile = str(form.get("ai_rule_profile") or "").strip()
    if not ai_rule_profile:
        errors.append("AI rule profile is required.")

    def _positive_int(name: str, field_label: str) -> int | None:
        raw = str(form.get(name) or "").strip()
        try:
            value = int(raw)
            if value <= 0:
                errors.append(f"{field_label} must be a positive integer.")
                return None
            return value
        except ValueError:
            errors.append(f"{field_label} must be a positive integer.")
            return None

    def _non_negative_int(name: str, field_label: str) -> int | None:
        raw = str(form.get(name) or "").strip()
        try:
            value = int(raw)
            if value < 0:
                errors.append(f"{field_label} must be a non-negative integer.")
                return None
            return value
        except ValueError:
            errors.append(f"{field_label} must be a non-negative integer.")
            return None

    min_duration = _positive_int("min_duration_minutes", "Min duration minutes")
    max_duration = _positive_int("max_duration_minutes", "Max duration minutes")
    max_downloads = _positive_int("max_downloads_per_run", "Max downloads per run")
    max_clips = _positive_int("max_clips", "Max clips")
    min_clip = _positive_int("min_clip_duration_sec", "Min clip duration")
    max_clip = _positive_int("max_clip_duration_sec", "Max clip duration")
    max_overlap = _non_negative_int("max_overlap_sec", "Max overlap seconds")

    if (
        min_duration is not None
        and max_duration is not None
        and min_duration >= max_duration
    ):
        errors.append("Min duration minutes must be less than max duration minutes.")

    if min_clip is not None and max_clip is not None and min_clip >= max_clip:
        errors.append("Min clip duration must be less than max clip duration.")

    acquisition_source_type = str(form.get("acquisition_source_type") or "").strip()
    if not acquisition_source_type:
        errors.append("Acquisition source type is required.")
    elif (
        type_error := validate_acquisition_source_type(
            acquisition_source_type, field="Acquisition source type"
        )
    ):
        errors.append(type_error)

    filename_prefix = str(form.get("filename_prefix") or "").strip()
    if not filename_prefix:
        errors.append("Filename prefix is required.")

    sources = _parse_sources(form, errors)
    routes = _parse_routes(form, errors)

    target_platforms = [
        platform
        for platform in sorted(ALLOWED_PLATFORMS)
        if _checkbox_on(form, f"target_platform_{platform}")
    ]

    platforms = {
        platform: _checkbox_on(form, f"platform_{platform}")
        for platform in sorted(ALLOWED_PLATFORMS)
    }

    config_manager_funnel_id = str(form.get("config_manager_funnel_id") or "").strip() or None

    ai_rules_payload: dict[str, Any] = {
        "ai_rule_profile": ai_rule_profile,
        "prompt_managed": existing.processing.ai_rules.prompt_managed,
    }
    if existing.processing.ai_rules.prompt_managed == "custom":
        ai_rules_payload["prompt_text"] = existing.processing.ai_rules.prompt_text

    if errors:
        return None, errors

    identity = payload["identity"]
    identity["funnel_id"] = original_funnel_id
    identity["display_name"] = display_name
    identity["description"] = str(form.get("description") or "").strip() or None
    identity["category"] = str(form.get("category") or "").strip() or None
    identity["enabled"] = _checkbox_on(form, "enabled")
    identity["environment"] = environment
    identity["status"] = status
    identity["created_at"] = existing.identity.created_at
    identity["template_source"] = existing.identity.template_source
    identity["updated_at"] = _utc_now_iso()
    identity["operator_note"] = str(form.get("operator_note") or "").strip() or None

    payload["acquisition"] = {
        "source_type": acquisition_source_type,
        "sources": sources,
        "min_duration_minutes": min_duration,
        "max_duration_minutes": max_duration,
        "max_downloads_per_run": max_downloads,
    }

    payload["processing"] = {
        "pipeline_profile": pipeline_profile,
        "ai_rules": ai_rules_payload,
        "selection": {
            "max_clips": max_clips,
            "min_clip_duration_sec": min_clip,
            "max_clip_duration_sec": max_clip,
            "max_overlap_sec": max_overlap,
        },
        "output": {
            "filename_prefix": filename_prefix,
            "delivery_mode": delivery_mode,
        },
        "platforms": platforms,
    }

    payload["distribution"] = {
        "posting_enabled": _checkbox_on(form, "posting_enabled"),
        "posting_mode": posting_mode,
        "target_platforms": target_platforms,
        "channel_routes": routes,
    }

    payload["mappings"] = {
        "config_manager_funnel_id": config_manager_funnel_id,
        "config_manager_preset_id": existing.mappings.config_manager_preset_id,
    }

    try:
        return load_canonical_funnel(payload), []
    except CanonicalFunnelSchemaError as exc:
        return None, [str(exc)]


def save_edited_funnel_in_registry(
    existing: CanonicalFunnel,
    form: Mapping[str, Any],
    registry: FunnelRegistry,
) -> CanonicalFunnel:
    """Apply form edits and save the updated funnel to the registry only."""
    updated, errors = update_funnel_from_form(existing, form)
    if updated is None or errors:
        raise FunnelEditError("; ".join(errors) if errors else "Could not update funnel.")

    registry.save_funnel(updated, overwrite=True)
    return updated
