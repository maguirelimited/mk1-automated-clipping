"""ConfigManager YAML projection helpers for funnel sync."""

from __future__ import annotations

import copy
from typing import Any

import yaml

from .schema import (
    DEFAULT_CONFIG_MANAGER_PRESET,
    CanonicalFunnel,
)

CANONICAL_TO_CONFIG_MANAGER_PLATFORM = {
    "youtube_shorts": "youtube",
    "youtube": "youtube",
    "tiktok": "tiktok",
    "instagram_reels": "instagram",
    "facebook_reels": "facebook",
    "x": "x",
}

OWNED_FUNNEL_KEYS = frozenset({"id", "name", "preset", "enabled"})


def resolve_config_manager_funnel_id(funnel: CanonicalFunnel) -> str:
    mapping = funnel.mappings.config_manager_funnel_id
    if mapping:
        return mapping
    return funnel.identity.funnel_id


def resolve_config_manager_preset(funnel: CanonicalFunnel) -> str:
    return funnel.mappings.config_manager_preset_id or DEFAULT_CONFIG_MANAGER_PRESET


def config_manager_platforms(funnel: CanonicalFunnel) -> list[str]:
    enabled: list[str] = []
    seen: set[str] = set()
    for platform, is_enabled in sorted(funnel.processing.platforms.items()):
        if not is_enabled:
            continue
        mapped = CANONICAL_TO_CONFIG_MANAGER_PLATFORM.get(platform)
        if mapped and mapped not in seen:
            enabled.append(mapped)
            seen.add(mapped)
    for platform in funnel.distribution.target_platforms:
        mapped = CANONICAL_TO_CONFIG_MANAGER_PLATFORM.get(platform)
        if mapped and mapped not in seen:
            enabled.append(mapped)
            seen.add(mapped)
    if not enabled:
        return ["youtube"]
    return enabled


def _new_document(funnel: CanonicalFunnel) -> dict[str, Any]:
    funnel_id = resolve_config_manager_funnel_id(funnel)
    return {
        "funnel": {
            "id": funnel_id,
            "name": funnel.identity.display_name,
            "preset": resolve_config_manager_preset(funnel),
            "enabled": funnel.identity.enabled,
        },
        "sources": {"channels": [], "rules": []},
        "selection": {"preferred_topics": [], "blocked_topics": []},
        "platforms": {"enabled": config_manager_platforms(funnel)},
    }


def merge_config_manager_document(
    existing: dict[str, Any] | None,
    funnel: CanonicalFunnel,
) -> dict[str, Any]:
    """Merge owned ConfigManager keys while preserving unknown/custom sections."""
    doc = copy.deepcopy(existing) if existing else _new_document(funnel)
    funnel_id = resolve_config_manager_funnel_id(funnel)

    funnel_section = doc.setdefault("funnel", {})
    if not isinstance(funnel_section, dict):
        funnel_section = {}
        doc["funnel"] = funnel_section
    funnel_section["id"] = funnel_id
    funnel_section["name"] = funnel.identity.display_name
    funnel_section["preset"] = resolve_config_manager_preset(funnel)
    funnel_section["enabled"] = funnel.identity.enabled

    sources = doc.setdefault("sources", {})
    if not isinstance(sources, dict):
        sources = {}
        doc["sources"] = sources
    sources.setdefault("channels", [])
    sources.setdefault("rules", [])

    selection = doc.setdefault("selection", {})
    if not isinstance(selection, dict):
        selection = {}
        doc["selection"] = selection
    selection.setdefault("preferred_topics", [])
    selection.setdefault("blocked_topics", [])

    platforms = doc.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        platforms = {}
        doc["platforms"] = platforms
    platforms["enabled"] = config_manager_platforms(funnel)

    return doc


def load_config_manager_yaml(path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return None
    data = yaml.safe_load(raw)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"ConfigManager YAML root must be an object: {path}")
    return data


def dump_config_manager_yaml(document: dict[str, Any]) -> str:
    text = yaml.safe_dump(
        document,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    if not text.endswith("\n"):
        text += "\n"
    return text


def plan_config_manager_yaml(
    funnel: CanonicalFunnel,
    yaml_path,
) -> tuple[str, str | None, str, bool, list[str]]:
    """Return action, before_text, after_text, changed, messages."""
    funnel_id = resolve_config_manager_funnel_id(funnel)
    messages: list[str] = []

    try:
        existing_doc = load_config_manager_yaml(yaml_path)
    except ValueError as exc:
        return "error", None, "", False, [str(exc)]

    before_text = yaml_path.read_text(encoding="utf-8") if yaml_path.is_file() else None
    after_doc = merge_config_manager_document(existing_doc, funnel)
    after_text = dump_config_manager_yaml(after_doc)

    if before_text is None:
        return (
            "create",
            None,
            after_text,
            True,
            [f"Create ConfigManager YAML for funnel {funnel_id!r}."],
        )

    if before_text == after_text:
        return (
            "unchanged",
            before_text,
            after_text,
            False,
            [f"ConfigManager YAML {yaml_path.name} is already up to date."],
        )

    messages.append(f"Update ConfigManager YAML for funnel {funnel_id!r}.")
    return "update", before_text, after_text, True, messages
