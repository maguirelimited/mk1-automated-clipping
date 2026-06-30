"""Generic operator-settings field machinery for the Ops UI.

This is the reusable core behind every "saved-value -> env var -> built-in
default" settings block the Ops UI owns (local AI, processing, post-processing).
A block is just a tuple of :class:`ConfigField` plus a store prefix and a
``controls.json`` block key.

Design rules (identical to the local-AI block, generalised):
- Ops UI is the control plane. It writes the shared file; services read it.
- Saved values are stored as strings (like environment variables). Readers
  coerce them. Defaults here must match the service-side defaults exactly so an
  unsaved field behaves identically to "no override".
- Resolution order for every consuming service is:
  per-run option (where applicable) -> UI saved value -> env var -> default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConfigField:
    name: str
    label: str
    kind: str  # "choice" | "bool" | "text" | "int" | "float"
    default: Any
    env_var: str
    help: str
    choices: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    group: str = ""


# A boolean field is rendered as a true/false choice so it reuses the existing
# <select> markup and the same string storage as every other field.
BOOL_CHOICES = ("true", "false")


def _bool_choices_for(field: ConfigField) -> tuple[str, ...]:
    return field.choices or BOOL_CHOICES


def coerce(field: ConfigField, raw: str) -> Any:
    text = (raw or "").strip()
    if text == "":
        return field.default
    if field.kind == "choice":
        return text if text in (field.choices or ()) else field.default
    if field.kind == "bool":
        lowered = text.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return field.default
    if field.kind == "int":
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return field.default
    if field.kind == "float":
        try:
            return float(text)
        except (TypeError, ValueError):
            return field.default
    return text


def effective_value(field: ConfigField, saved: dict[str, str]) -> Any:
    """Resolve one field: saved UI value -> env var -> built-in default."""
    if field.name in saved and str(saved.get(field.name) or "").strip() != "":
        return coerce(field, str(saved.get(field.name)))
    env_raw = os.environ.get(field.env_var, "")
    if env_raw is not None and str(env_raw).strip() != "":
        return coerce(field, str(env_raw))
    return field.default


def effective_config(fields: tuple[ConfigField, ...], saved: dict[str, str]) -> dict[str, Any]:
    return {field.name: effective_value(field, saved) for field in fields}


def source_for(
    fields_by_name: dict[str, ConfigField],
    field_name: str,
    saved: dict[str, str],
) -> str:
    """Where the effective value comes from: 'ui', 'env', or 'default'."""
    field = fields_by_name.get(field_name)
    if field is None:
        return "default"
    if field.name in saved and str(saved.get(field.name) or "").strip() != "":
        return "ui"
    env_raw = os.environ.get(field.env_var, "")
    if env_raw is not None and str(env_raw).strip() != "":
        return "env"
    return "default"


def parse_form(
    fields: tuple[ConfigField, ...],
    form: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    """Validate a submitted settings form for one block.

    Returns ``(values, errors)`` where ``values`` maps field name -> string to
    persist. On any error the field is skipped and an error message is added.
    """
    values: dict[str, str] = {}
    errors: list[str] = []
    for field in fields:
        if field.name not in form:
            continue
        raw = str(form.get(field.name) or "").strip()
        if raw == "":
            errors.append(f"{field.label}: value is required.")
            continue
        if field.kind in ("choice", "bool"):
            allowed = _bool_choices_for(field)
            if raw not in allowed:
                errors.append(f"{field.label}: must be one of {', '.join(allowed)}.")
                continue
            values[field.name] = raw
            continue
        if field.kind in ("int", "float"):
            try:
                number = float(raw)
            except (TypeError, ValueError):
                errors.append(f"{field.label}: must be a number.")
                continue
            if field.minimum is not None and number < field.minimum:
                errors.append(f"{field.label}: must be >= {field.minimum:g}.")
                continue
            if field.maximum is not None and number > field.maximum:
                errors.append(f"{field.label}: must be <= {field.maximum:g}.")
                continue
            values[field.name] = str(int(number)) if field.kind == "int" else repr(number)
            continue
        values[field.name] = raw
    return values, errors


def fields_view(
    fields: tuple[ConfigField, ...],
    fields_by_name: dict[str, ConfigField],
    saved: dict[str, str],
) -> list[dict[str, Any]]:
    """Build the template-friendly view list for a settings block."""
    effective = effective_config(fields, saved)
    view: list[dict[str, Any]] = []
    for field in fields:
        value = effective.get(field.name)
        if field.kind == "bool":
            kind = "choice"
            choices = _bool_choices_for(field)
            value = "true" if value else "false"
        else:
            kind = field.kind
            choices = field.choices
        view.append(
            {
                "name": field.name,
                "label": field.label,
                "kind": kind,
                "choices": choices,
                "help": field.help,
                "value": value,
                "source": source_for(fields_by_name, field.name, saved),
                "env_var": field.env_var,
                "group": field.group,
            }
        )
    return view
