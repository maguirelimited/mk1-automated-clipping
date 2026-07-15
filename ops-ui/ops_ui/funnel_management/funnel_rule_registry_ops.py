"""Ops UI helpers for ai-service/config/funnel_rule_registry.json."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

REGISTRY_SCHEMA_VERSION = 1
PROFILE_ID_RE = re.compile(r"^[a-z0-9_]+$")

BUILTIN_PROFILES: dict[str, dict[str, str]] = {
    "business": {"rules_version": "business_v1", "managed": "builtin"},
    "finance": {"rules_version": "finance_v1", "managed": "builtin"},
    "sport": {"rules_version": "sport_v1", "managed": "builtin"},
    "comedy": {"rules_version": "comedy_v1", "managed": "builtin"},
}

BUILTIN_ALIASES: dict[str, str] = {
    "business": "business",
    "business_ai": "business",
    "mfm_business_ai_001": "business",
    "finance": "finance",
    "sport": "sport",
    "sports": "sport",
    "comedy": "comedy",
}


class FunnelRuleRegistryOpsError(Exception):
    """Raised when the funnel rule registry cannot be read or validated."""


def default_registry_document() -> dict[str, Any]:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "profiles": copy.deepcopy(BUILTIN_PROFILES),
        "aliases": copy.deepcopy(BUILTIN_ALIASES),
    }


def load_registry_document(path: Path) -> dict[str, Any]:
    """Load registry JSON strictly; raise on missing, malformed, or invalid content."""
    if not path.is_file():
        raise FunnelRuleRegistryOpsError(f"Funnel rule registry file is missing: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except PermissionError as exc:
        raise FunnelRuleRegistryOpsError(
            f"Funnel rule registry is not readable (permission denied): {path}"
        ) from exc
    except OSError as exc:
        raise FunnelRuleRegistryOpsError(
            f"Funnel rule registry could not be read: {path} ({exc})"
        ) from exc
    except json.JSONDecodeError as exc:
        raise FunnelRuleRegistryOpsError(
            f"Invalid JSON in funnel rule registry {path.name}: {exc.msg}"
        ) from exc
    validate_registry_document(raw)
    return raw


def validate_registry_document(data: Any) -> None:
    if not isinstance(data, dict):
        raise FunnelRuleRegistryOpsError("Funnel rule registry root must be a JSON object")
    schema_version = data.get("schema_version")
    if schema_version != REGISTRY_SCHEMA_VERSION:
        raise FunnelRuleRegistryOpsError(
            f"Unsupported funnel rule registry schema_version {schema_version!r}; "
            f"expected {REGISTRY_SCHEMA_VERSION}"
        )

    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise FunnelRuleRegistryOpsError("Funnel rule registry requires a non-empty profiles object")

    for profile_id, item in profiles.items():
        clean_id = str(profile_id or "").strip()
        if not clean_id:
            raise FunnelRuleRegistryOpsError("Funnel rule registry profile keys must be non-empty")
        if not isinstance(item, dict):
            raise FunnelRuleRegistryOpsError(f"profiles[{clean_id!r}] must be an object")
        rules_version = str(item.get("rules_version") or "").strip()
        if not rules_version:
            raise FunnelRuleRegistryOpsError(f"profiles[{clean_id!r}] requires rules_version")

    aliases = data.get("aliases")
    if aliases is None:
        raise FunnelRuleRegistryOpsError("Funnel rule registry requires an aliases object")
    if not isinstance(aliases, dict):
        raise FunnelRuleRegistryOpsError("Funnel rule registry aliases must be an object")

    profile_ids = {str(key).strip() for key in profiles}
    for alias, profile_id in aliases.items():
        clean_alias = str(alias or "").strip()
        clean_profile = str(profile_id or "").strip()
        if not clean_alias or not clean_profile:
            raise FunnelRuleRegistryOpsError("Funnel rule registry alias entries must be non-empty strings")
        if clean_profile not in profile_ids:
            raise FunnelRuleRegistryOpsError(
                f"aliases[{clean_alias!r}] references unknown profile {clean_profile!r}"
            )


def normalize_aliases(raw_aliases: dict[Any, Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for key, value in raw_aliases.items():
        alias = str(key or "").strip().lower()
        profile_id = str(value or "").strip()
        if alias and profile_id:
            aliases[alias] = profile_id
    return aliases


def list_registry_profile_ids(path: Path | None) -> list[str]:
    """Return sorted profile IDs from the funnel rule registry, or built-in defaults."""
    if path is not None:
        try:
            registry = load_registry_document(path)
            profiles = registry.get("profiles")
            if isinstance(profiles, dict) and profiles:
                return sorted(str(key) for key in profiles)
        except FunnelRuleRegistryOpsError:
            pass
    return sorted(BUILTIN_PROFILES)


def derive_rules_version(profile_id: str) -> str:
    return f"{profile_id}_v1"


def validate_profile_id(profile_id: str) -> str | None:
    clean = str(profile_id or "").strip()
    if not clean or not PROFILE_ID_RE.match(clean):
        return f"AI rule profile {profile_id!r} must contain only lowercase letters, numbers, and underscores."
    return None


def get_profile_entry(registry: dict[str, Any], profile_id: str) -> dict[str, Any] | None:
    profiles = registry.get("profiles")
    if not isinstance(profiles, dict):
        return None
    item = profiles.get(profile_id)
    return item if isinstance(item, dict) else None


def get_profile_rules_version(registry: dict[str, Any], profile_id: str) -> str | None:
    item = get_profile_entry(registry, profile_id)
    if item is None:
        return None
    rules_version = str(item.get("rules_version") or "").strip()
    return rules_version or None


def get_profile_managed(registry: dict[str, Any], profile_id: str) -> str | None:
    item = get_profile_entry(registry, profile_id)
    if item is None:
        return None
    managed = str(item.get("managed") or "builtin").strip().lower()
    return managed or "builtin"


def validate_profile_and_prompt(
    registry: dict[str, Any],
    *,
    profile_id: str,
    prompts_dir: Path | None,
) -> list[str]:
    """Return blocking error messages for unknown profile or missing prompt file."""
    errors: list[str] = []
    rules_version = get_profile_rules_version(registry, profile_id)
    if rules_version is None:
        errors.append(f"AI rule profile {profile_id!r} was not found in funnel rule registry profiles.")
        return errors

    if prompts_dir is None:
        return errors

    prompt_path = prompts_dir / f"{rules_version}.txt"
    if not prompt_path.is_file():
        errors.append(
            f"AI prompt file missing for profile {profile_id!r} (expected {prompt_path.name})."
        )
    return errors


def _patch_alias(registry: dict[str, Any], *, funnel_id: str, profile_id: str) -> tuple[bool, list[str]]:
    aliases_raw = registry.get("aliases")
    if not isinstance(aliases_raw, dict):
        return False, ["Funnel rule registry aliases must be an object."]

    aliases = normalize_aliases(aliases_raw)
    lookup_id = funnel_id.strip().lower()
    current = aliases.get(lookup_id)
    if current == profile_id:
        return False, [f"AI alias maps {funnel_id!r} to profile {profile_id!r}."]

    after_aliases = registry.setdefault("aliases", {})
    if not isinstance(after_aliases, dict):
        after_aliases = {}
        registry["aliases"] = after_aliases
    after_aliases[funnel_id] = profile_id

    if current is None:
        return True, [f"Create alias aliases[{funnel_id!r}] = {profile_id!r}."]
    return True, [f"Update alias aliases[{funnel_id!r}] from {current!r} to {profile_id!r}."]


def plan_alias_patch(
    registry: dict[str, Any],
    *,
    funnel_id: str,
    profile_id: str,
) -> tuple[str, dict[str, Any], bool, list[str]]:
    """Plan creating or updating a funnel_id → profile alias only.

    Returns ``(action, after_registry, changed, messages)`` where action is
    ``create``, ``update``, ``unchanged``, or ``error``.
    """
    profiles = registry.get("profiles")
    if not isinstance(profiles, dict) or profile_id not in profiles:
        return (
            "error",
            copy.deepcopy(registry),
            False,
            [f"Profile {profile_id!r} not found in funnel rule registry."],
        )

    after = copy.deepcopy(registry)
    aliases_raw = after.get("aliases") if isinstance(after.get("aliases"), dict) else {}
    current = normalize_aliases(aliases_raw).get(funnel_id.strip().lower())
    changed, messages = _patch_alias(after, funnel_id=funnel_id, profile_id=profile_id)
    if not changed:
        return "unchanged", after, False, messages
    action = "create" if current is None else "update"
    return action, after, True, messages


def plan_builtin_registry_sync(
    registry: dict[str, Any],
    *,
    funnel_id: str,
    profile_id: str,
    prompts_dir: Path | None,
) -> tuple[str, dict[str, Any], bool, list[str]]:
    profile_errors = validate_profile_and_prompt(registry, profile_id=profile_id, prompts_dir=prompts_dir)
    if profile_errors:
        return "error", registry, False, profile_errors

    after = copy.deepcopy(registry)
    alias_changed, messages = _patch_alias(after, funnel_id=funnel_id, profile_id=profile_id)
    if alias_changed:
        action = "update"
        return action, after, True, messages

    return "unchanged", after, False, messages


def plan_custom_registry_sync(
    registry: dict[str, Any],
    *,
    funnel_id: str,
    profile_id: str,
    rules_version: str,
) -> tuple[str, dict[str, Any], bool, list[str]]:
    profile_error = validate_profile_id(profile_id)
    if profile_error:
        return "error", registry, False, [profile_error]

    existing = get_profile_entry(registry, profile_id)
    messages: list[str] = []
    profile_changed = False

    if existing is not None:
        managed = get_profile_managed(registry, profile_id) or "builtin"
        if managed == "builtin":
            return (
                "error",
                registry,
                False,
                [f"Refusing to overwrite built-in AI profile {profile_id!r}."],
            )
        current_version = str(existing.get("rules_version") or "").strip()
        if current_version != rules_version:
            profile_changed = True
            messages.append(
                f"Update profile {profile_id!r} rules_version from {current_version!r} to {rules_version!r}."
            )
    else:
        profile_changed = True
        messages.append(f"Create profile {profile_id!r} with rules_version {rules_version!r}.")

    after = copy.deepcopy(registry)
    profiles = after.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
        after["profiles"] = profiles
    profiles[profile_id] = {"rules_version": rules_version, "managed": "ops_ui"}

    alias_changed, alias_messages = _patch_alias(after, funnel_id=funnel_id, profile_id=profile_id)
    messages.extend(alias_messages)

    changed = profile_changed or alias_changed
    if not changed:
        return "unchanged", after, False, messages

    action = "create" if existing is None else "update"
    return action, after, True, messages


def plan_prompt_file_sync(
    *,
    prompts_dir: Path | None,
    rules_version: str,
    prompt_text: str,
    prompt_managed: str,
    registry: dict[str, Any],
    profile_id: str,
) -> tuple[str, Path | None, str | None, str, bool, list[str]]:
    """Return action, path, before_text, after_text, changed, messages."""
    if prompt_managed != "custom":
        return (
            "skipped",
            None,
            None,
            "",
            False,
            ["Builtin AI profiles do not write prompt files during sync."],
        )

    if prompts_dir is None:
        return "error", None, None, "", False, ["AI prompts directory was not supplied."]

    if not str(prompt_text or "").strip():
        return "error", None, None, "", False, ["Custom AI profile requires prompt text."]

    path = prompts_dir / f"{rules_version}.txt"
    after_text = prompt_text if prompt_text.endswith("\n") else f"{prompt_text}\n"

    if path.is_file():
        managed = get_profile_managed(registry, profile_id)
        if managed == "builtin":
            return (
                "error",
                path,
                path.read_text(encoding="utf-8"),
                after_text,
                False,
                [f"Refusing to overwrite built-in prompt file {path.name}."],
            )
        before_text = path.read_text(encoding="utf-8")
        if before_text == after_text:
            return (
                "unchanged",
                path,
                before_text,
                after_text,
                False,
                [f"Prompt file {path.name} is already up to date."],
            )
        return (
            "update",
            path,
            before_text,
            after_text,
            True,
            [f"Update custom prompt file {path.name}."],
        )

    return (
        "create",
        path,
        None,
        after_text,
        True,
        [f"Create custom prompt file {path.name}."],
    )
