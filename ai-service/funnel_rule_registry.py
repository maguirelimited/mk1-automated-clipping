"""Config-driven funnel rule profile registry for section candidate discovery."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REGISTRY_SCHEMA_VERSION = 1
DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent / "config" / "funnel_rule_registry.json"

BUILTIN_ALIASES: dict[str, str] = {
    "business": "business",
    "business_ai": "business",
    "mfm_business_ai_001": "business",
    "finance": "finance",
    "sport": "sport",
    "sports": "sport",
    "comedy": "comedy",
}

BUILTIN_PROFILES: dict[str, dict[str, str]] = {
    "business": {"rules_version": "business_v1", "managed": "builtin"},
    "finance": {"rules_version": "finance_v1", "managed": "builtin"},
    "sport": {"rules_version": "sport_v1", "managed": "builtin"},
    "comedy": {"rules_version": "comedy_v1", "managed": "builtin"},
}


@dataclass(frozen=True)
class ProfileSpec:
    rules_version: str
    managed: str


@dataclass(frozen=True)
class FunnelRuleRegistry:
    aliases: dict[str, str]
    profiles: dict[str, ProfileSpec]
    source: str


_registry: FunnelRuleRegistry | None = None
_registry_load_status: str = "uninitialized"


def get_registry_load_status() -> str:
    """Return how the active registry was loaded: file, fallback_missing, or fallback_invalid."""
    if _registry is None:
        reload_funnel_rule_registry()
    return _registry_load_status


def _normalize_alias_map(raw: Any, *, label: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    aliases: dict[str, str] = {}
    for key, value in raw.items():
        alias = str(key or "").strip().lower()
        profile_id = str(value or "").strip()
        if not alias or not profile_id:
            raise ValueError(f"{label} entries must be non-empty strings")
        aliases[alias] = profile_id
    return aliases


def _normalize_profiles(raw: Any) -> dict[str, ProfileSpec]:
    if not isinstance(raw, dict):
        raise ValueError("profiles must be an object")
    profiles: dict[str, ProfileSpec] = {}
    for profile_id, item in raw.items():
        clean_id = str(profile_id or "").strip()
        if not clean_id:
            raise ValueError("profiles keys must be non-empty strings")
        if not isinstance(item, dict):
            raise ValueError(f"profiles[{clean_id!r}] must be an object")
        rules_version = str(item.get("rules_version") or "").strip()
        managed = str(item.get("managed") or "builtin").strip().lower() or "builtin"
        if not rules_version:
            raise ValueError(f"profiles[{clean_id!r}] requires rules_version")
        if managed not in {"builtin", "ops_ui"}:
            raise ValueError(
                f"profiles[{clean_id!r}].managed must be 'builtin' or 'ops_ui', got {managed!r}"
            )
        profiles[clean_id] = ProfileSpec(rules_version=rules_version, managed=managed)
    return profiles


def _validate_registry(aliases: dict[str, str], profiles: dict[str, ProfileSpec]) -> None:
    for alias, profile_id in aliases.items():
        if profile_id not in profiles:
            raise ValueError(
                f"aliases[{alias!r}] references unknown profile {profile_id!r}"
            )


def _builtin_registry(*, source: str) -> FunnelRuleRegistry:
    profiles = {
        profile_id: ProfileSpec(
            rules_version=spec["rules_version"],
            managed=spec["managed"],
        )
        for profile_id, spec in BUILTIN_PROFILES.items()
    }
    return FunnelRuleRegistry(
        aliases=dict(BUILTIN_ALIASES),
        profiles=profiles,
        source=source,
    )


def _parse_registry_document(data: Any) -> FunnelRuleRegistry:
    if not isinstance(data, dict):
        raise ValueError("registry root must be an object")
    schema_version = data.get("schema_version")
    if schema_version != REGISTRY_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported schema_version {schema_version!r}; expected {REGISTRY_SCHEMA_VERSION}"
        )
    aliases = _normalize_alias_map(data.get("aliases"), label="aliases")
    profiles = _normalize_profiles(data.get("profiles"))
    _validate_registry(aliases, profiles)
    return FunnelRuleRegistry(aliases=aliases, profiles=profiles, source="file")


def load_funnel_rule_registry(path: Path | str | None = None) -> FunnelRuleRegistry:
    """Load the funnel rule registry from JSON, falling back to built-in defaults."""
    global _registry_load_status

    registry_path = Path(path).expanduser() if path is not None else DEFAULT_REGISTRY_PATH
    if not registry_path.is_file():
        _registry_load_status = "fallback_missing"
        return _builtin_registry(source="builtin")

    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
        registry = _parse_registry_document(raw)
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        _registry_load_status = "fallback_invalid"
        print(
            f"[funnel_rule_registry] WARNING: could not load {registry_path}: {exc}. "
            "Using built-in funnel rule registry.",
            file=sys.stderr,
        )
        return _builtin_registry(source="builtin")

    _registry_load_status = "file"
    return registry


def reload_funnel_rule_registry(path: Path | str | None = None) -> FunnelRuleRegistry:
    """Reload and cache the active funnel rule registry."""
    global _registry
    _registry = load_funnel_rule_registry(path)
    return _registry


def get_active_funnel_rule_registry() -> FunnelRuleRegistry:
    if _registry is None:
        return reload_funnel_rule_registry()
    return _registry


def get_funnel_rule_aliases() -> dict[str, str]:
    return dict(get_active_funnel_rule_registry().aliases)


def get_funnel_rule_versions() -> dict[str, str]:
    registry = get_active_funnel_rule_registry()
    return {
        profile_id: spec.rules_version
        for profile_id, spec in registry.profiles.items()
    }


def resolve_profile_id(funnel_id: str) -> str:
    aliases = get_funnel_rule_aliases()
    return aliases[funnel_id]


def resolve_rules_version(profile_id: str) -> str:
    registry = get_active_funnel_rule_registry()
    spec = registry.profiles.get(profile_id)
    if spec is None:
        raise KeyError(profile_id)
    return spec.rules_version


def get_profile_spec(profile_id: str) -> ProfileSpec | None:
    return get_active_funnel_rule_registry().profiles.get(profile_id)
