"""Create-funnel helpers (Funnel Management MK1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

from .acquisition_sources import (
    default_per_source_type,
    validate_acquisition_source_type,
    validate_per_source_type,
)
from .create_defaults import (
    BASELINE_AI_RULE_PROFILE,
    BASELINE_CREATE_ENABLED,
    BASELINE_CREATE_STATUS,
    BASELINE_ENVIRONMENT,
    BASELINE_TEMPLATE_ID,
    DEFAULT_CREATE_CONFIG_MANAGER_PRESET,
    DEFAULT_CREATE_MAX_VIDEOS_PER_SOURCE,
)
from .funnel_templates import FunnelTemplateError, build_funnel_from_template, get_funnel_template
from .registry import DuplicateFunnelError, FunnelRegistry
from .schema import (
    CanonicalFunnel,
    CanonicalFunnelSchemaError,
    dump_canonical_funnel,
    load_canonical_funnel,
)

_SOURCE_ID_RE = re.compile(r"^[a-z0-9_]+$")


class FunnelCreateError(ValueError):
    """Raised when create form data cannot produce a registry funnel."""


@dataclass(frozen=True)
class FunnelCreateForm:
    template_id: str
    funnel_id: str
    display_name: str
    category: str
    source_type: str
    source_urls: tuple[str, ...]
    description: str | None = None


def form_values_from_request(form: Mapping[str, Any]) -> dict[str, str]:
    """Extract raw form values for re-rendering after validation errors."""
    return {
        "template_id": str(form.get("template_id") or BASELINE_TEMPLATE_ID).strip(),
        "funnel_id": str(form.get("funnel_id") or "").strip(),
        "display_name": str(form.get("display_name") or "").strip(),
        "category": str(form.get("category") or "").strip(),
        "source_type": str(form.get("source_type") or "").strip(),
        "source_urls": str(form.get("source_urls") or form.get("source_url") or "").strip(),
        "description": str(form.get("description") or "").strip(),
    }


def _parse_source_urls(raw: str) -> list[str]:
    urls: list[str] = []
    for line in raw.replace(",", "\n").split("\n"):
        url = line.strip()
        if url:
            urls.append(url)
    return urls


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_funnel_create_form(form: Mapping[str, Any]) -> tuple[FunnelCreateForm | None, list[str]]:
    """Parse and validate the simplified create-funnel form."""
    errors: list[str] = []
    template_id = str(form.get("template_id") or BASELINE_TEMPLATE_ID).strip() or BASELINE_TEMPLATE_ID
    funnel_id = str(form.get("funnel_id") or "").strip()
    display_name = str(form.get("display_name") or "").strip()
    category = str(form.get("category") or "").strip()
    source_type = str(form.get("source_type") or "").strip().lower()
    description = str(form.get("description") or "").strip() or None
    source_urls = _parse_source_urls(str(form.get("source_urls") or form.get("source_url") or ""))

    if not funnel_id:
        errors.append("Funnel ID is required.")
    if not display_name:
        errors.append("Display name is required.")
    if not category:
        errors.append("Niche / category is required.")
    if not source_type:
        errors.append("Source type is required.")
    elif (type_error := validate_acquisition_source_type(source_type, field="Source type")):
        errors.append(type_error)
    elif (type_error := validate_per_source_type(source_type, field="Source type")):
        errors.append(type_error)
    if not source_urls:
        errors.append("At least one source URL is required.")
    else:
        for index, url in enumerate(source_urls, start=1):
            if not _looks_like_url(url):
                errors.append(f"Source URL {index} is not a valid http(s) URL.")

    if errors:
        return None, errors

    try:
        get_funnel_template(template_id)
    except FunnelTemplateError as exc:
        return None, [str(exc)]

    if not _SOURCE_ID_RE.match(funnel_id):
        errors.append("Funnel ID must contain only lowercase letters, numbers, and underscores.")

    if errors:
        return None, errors

    return (
        FunnelCreateForm(
            template_id=template_id,
            funnel_id=funnel_id,
            display_name=display_name,
            category=category,
            source_type=source_type,
            source_urls=tuple(source_urls),
            description=description,
        ),
        [],
    )


def _derive_source_id(*, funnel_id: str, index: int) -> str:
    suffix = "" if index == 0 else f"_{index + 1}"
    return f"{funnel_id}_source{suffix}"[:128]


def _derive_source_label(url: str, *, index: int) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/").split("/")[-1] if parsed.path.strip("/") else parsed.netloc
    label = path.replace("-", " ").replace("_", " ").strip() or parsed.netloc
    if index:
        return f"{label.title()} {index + 1}"
    return label.title() or f"Source {index + 1}"


def _build_sources(form: FunnelCreateForm) -> list[dict[str, Any]]:
    per_source_type = default_per_source_type(form.source_type)
    sources: list[dict[str, Any]] = []
    for index, url in enumerate(form.source_urls):
        sources.append(
            {
                "source_id": _derive_source_id(funnel_id=form.funnel_id, index=index),
                "label": _derive_source_label(url, index=index),
                "url": url,
                "source_type": per_source_type,
                "active": True,
                "max_videos_per_source": DEFAULT_CREATE_MAX_VIDEOS_PER_SOURCE,
                "hydrate_missing_duration": True,
                "title_allowlist": [],
                "title_blocklist": [],
            }
        )
    return sources


def _apply_create_defaults(funnel: CanonicalFunnel, form: FunnelCreateForm) -> CanonicalFunnel:
    payload = dump_canonical_funnel(funnel)
    payload["identity"]["status"] = BASELINE_CREATE_STATUS
    payload["identity"]["enabled"] = BASELINE_CREATE_ENABLED
    payload["identity"]["environment"] = BASELINE_ENVIRONMENT
    payload["acquisition"]["source_type"] = form.source_type
    payload["processing"]["ai_rules"] = {
        "ai_rule_profile": BASELINE_AI_RULE_PROFILE,
        "prompt_managed": "builtin",
    }
    payload["mappings"] = {
        "config_manager_funnel_id": form.funnel_id,
        "config_manager_preset_id": DEFAULT_CREATE_CONFIG_MANAGER_PRESET,
    }
    return load_canonical_funnel(payload)


def create_funnel_in_registry(
    form: FunnelCreateForm,
    registry: FunnelRegistry,
) -> CanonicalFunnel:
    """Build a baseline funnel from a template and save it to the registry only."""
    try:
        funnel = build_funnel_from_template(
            form.template_id,
            funnel_id=form.funnel_id,
            display_name=form.display_name,
            environment=BASELINE_ENVIRONMENT,
            description=form.description,
            category=form.category,
            sources=_build_sources(form),
        )
        funnel = _apply_create_defaults(funnel, form)
    except (FunnelTemplateError, CanonicalFunnelSchemaError) as exc:
        raise FunnelCreateError(str(exc)) from exc

    try:
        registry.save_funnel(funnel, overwrite=False)
    except DuplicateFunnelError as exc:
        raise FunnelCreateError(
            f"A funnel with ID {form.funnel_id!r} already exists in the registry."
        ) from exc

    return funnel
