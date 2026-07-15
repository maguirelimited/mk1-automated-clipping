"""Clone-funnel helpers (Funnel Management MK1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from .registry import DuplicateFunnelError, FunnelRegistry
from .schema import (
    ALLOWED_ENVIRONMENTS,
    CanonicalFunnel,
    CanonicalFunnelSchemaError,
    dump_canonical_funnel,
    load_canonical_funnel,
)


class FunnelCloneError(ValueError):
    """Raised when a funnel cannot be cloned from form data or source config."""


@dataclass(frozen=True)
class FunnelCloneForm:
    source_funnel_id: str
    new_funnel_id: str
    display_name: str
    environment: str
    description: str | None = None
    category: str | None = None
    operator_note: str | None = None
    copy_sources: bool = True
    copy_distribution_routes: bool = True
    copy_mappings: bool = True


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _checkbox_on(form: Mapping[str, Any], name: str, *, default: bool = True) -> bool:
    if name not in form:
        return default
    return str(form.get(name) or "").strip().lower() in {"on", "true", "1", "yes"}


def clone_form_defaults(source: CanonicalFunnel) -> dict[str, str]:
    """Default clone form values for GET render."""
    return {
        "new_funnel_id": "",
        "display_name": f"Copy of {source.identity.display_name}",
        "environment": source.identity.environment,
        "description": source.identity.description or "",
        "category": source.identity.category or "",
        "operator_note": "",
        "copy_sources": "on",
        "copy_distribution_routes": "on",
        "copy_mappings": "on",
    }


def source_summary(source: CanonicalFunnel) -> dict[str, Any]:
    """Compact source funnel summary for the clone page."""
    return {
        "funnel_id": source.identity.funnel_id,
        "display_name": source.identity.display_name,
        "category": source.identity.category or "—",
        "environment": source.identity.environment,
        "status": source.identity.status,
        "enabled": source.identity.enabled,
        "source_count": len(source.acquisition.sources),
        "target_platforms_display": ", ".join(source.distribution.target_platforms) or "—",
        "route_count": len(source.distribution.channel_routes),
    }


def form_values_from_request(form: Mapping[str, Any]) -> dict[str, str]:
    keys = (
        "new_funnel_id",
        "display_name",
        "environment",
        "description",
        "category",
        "operator_note",
    )
    values = {key: str(form.get(key) or "").strip() for key in keys}
    values["copy_sources"] = "on" if _checkbox_on(form, "copy_sources") else ""
    values["copy_distribution_routes"] = "on" if _checkbox_on(form, "copy_distribution_routes") else ""
    values["copy_mappings"] = "on" if _checkbox_on(form, "copy_mappings") else ""
    return values


def parse_funnel_clone_form(
    form: Mapping[str, Any],
    *,
    source_funnel_id: str,
) -> tuple[FunnelCloneForm | None, list[str]]:
    errors: list[str] = []
    new_funnel_id = str(form.get("new_funnel_id") or "").strip()
    display_name = str(form.get("display_name") or "").strip()
    environment = str(form.get("environment") or "dev").strip().lower() or "dev"
    description = str(form.get("description") or "").strip() or None
    category = str(form.get("category") or "").strip() or None
    operator_note = str(form.get("operator_note") or "").strip() or None

    if not new_funnel_id:
        errors.append("New funnel ID is required.")
    if not display_name:
        errors.append("Display name is required.")
    if environment not in ALLOWED_ENVIRONMENTS:
        errors.append("Environment must be dev or prod.")
    if new_funnel_id and new_funnel_id == source_funnel_id:
        errors.append("New funnel ID must differ from the source funnel ID.")

    if errors:
        return None, errors

    return (
        FunnelCloneForm(
            source_funnel_id=source_funnel_id,
            new_funnel_id=new_funnel_id,
            display_name=display_name,
            environment=environment,
            description=description,
            category=category,
            operator_note=operator_note,
            copy_sources=_checkbox_on(form, "copy_sources"),
            copy_distribution_routes=_checkbox_on(form, "copy_distribution_routes"),
            copy_mappings=_checkbox_on(form, "copy_mappings"),
        ),
        [],
    )


def clone_canonical_funnel(
    source: CanonicalFunnel,
    *,
    new_funnel_id: str,
    display_name: str,
    environment: str | None = None,
    description: str | None = None,
    category: str | None = None,
    operator_note: str | None = None,
    copy_sources: bool = True,
    copy_distribution_routes: bool = True,
    copy_mappings: bool = True,
) -> CanonicalFunnel:
    """Build a new draft canonical funnel copied from an existing one."""
    payload = dump_canonical_funnel(source)
    now = _utc_now_iso()
    source_id = source.identity.funnel_id
    resolved_env = (environment or source.identity.environment).strip().lower()

    identity = payload["identity"]
    identity["funnel_id"] = new_funnel_id
    identity["display_name"] = display_name.strip()
    identity["description"] = description if description is not None else source.identity.description
    identity["category"] = category if category is not None else source.identity.category
    identity["enabled"] = False
    identity["environment"] = resolved_env
    identity["status"] = "draft"
    identity["template_source"] = f"clone:{source_id}"
    identity["created_at"] = now
    identity["updated_at"] = now
    identity["operator_note"] = operator_note

    processing = payload["processing"]
    if processing.get("pipeline_profile") == source_id:
        processing["pipeline_profile"] = new_funnel_id

    if not copy_sources:
        payload["acquisition"]["sources"] = []

    distribution = payload["distribution"]
    distribution["posting_enabled"] = False
    if not copy_distribution_routes:
        distribution["channel_routes"] = []

    if not copy_mappings:
        payload["mappings"]["config_manager_funnel_id"] = None

    try:
        return load_canonical_funnel(payload)
    except CanonicalFunnelSchemaError as exc:
        raise FunnelCloneError(f"Cloned funnel failed schema validation: {exc}") from exc


def save_cloned_funnel_in_registry(
    source: CanonicalFunnel,
    form: FunnelCloneForm,
    registry: FunnelRegistry,
) -> CanonicalFunnel:
    """Clone a funnel and save the result to the registry only."""
    try:
        cloned = clone_canonical_funnel(
            source,
            new_funnel_id=form.new_funnel_id,
            display_name=form.display_name,
            environment=form.environment,
            description=form.description,
            category=form.category,
            operator_note=form.operator_note,
            copy_sources=form.copy_sources,
            copy_distribution_routes=form.copy_distribution_routes,
            copy_mappings=form.copy_mappings,
        )
    except FunnelCloneError as exc:
        raise FunnelCloneError(str(exc)) from exc

    try:
        registry.save_funnel(cloned, overwrite=False)
    except DuplicateFunnelError as exc:
        raise FunnelCloneError(
            f"A funnel with ID {form.new_funnel_id!r} already exists in the registry."
        ) from exc

    return cloned
