"""Conditional production secret validation.

Enforced when ConfigManager runtime.require_production_secrets is true.
Requirements are derived from enabled functionality only:

- Local AI (ai_service / ollama): OPENAI_API_KEY is NOT required.
- OpenAI-backed clip selection: OPENAI_API_KEY must be present and non-placeholder.
- Uploading disabled or dry_run: platform posting credentials are NOT required.
- Real uploading with enabled destinations: credentials named by channel profiles
  must resolve to non-empty, non-placeholder values / readable files.

Never logs secret values — only variable names and file paths.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "change_me",
        "replace-me",
        "replace_me",
        "todo",
        "tbd",
        "xxx",
        "your-key-here",
        "your_key_here",
        "insert-key-here",
        "sk-placeholder",
        "example",
        "none",
        "null",
    }
)

_AI_SERVICE_ALIASES = frozenset({"ai_service", "ai-service", "local", "ollama", ""})


@dataclass
class SecretCheckResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    required_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "required_names": list(self.required_names),
        }


def _is_placeholder(value: str) -> bool:
    token = (value or "").strip()
    if not token:
        return True
    return token.lower() in PLACEHOLDER_VALUES


def _normalize_backend(raw: str) -> str:
    backend = (raw or "").strip().lower()
    if not backend or backend in _AI_SERVICE_ALIASES:
        return "ai_service"
    return "openai"


def resolve_clip_selection_backend_for_secrets() -> str:
    """Resolve clip-selection backend without importing video-automation."""
    # Prefer Ops UI controls when present.
    controls_path = os.environ.get("MK04_CONTROLS_FILE", "").strip()
    if controls_path:
        path = Path(controls_path).expanduser()
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                ai = payload.get("ai_config")
                if isinstance(ai, dict):
                    ui_backend = _normalize_backend(str(ai.get("clip_selection_backend") or ""))
                    if ui_backend:
                        return ui_backend
    env_backend = _normalize_backend(os.environ.get("CLIP_SELECTION_BACKEND", ""))
    if env_backend:
        return env_backend
    # AI_PROVIDER=openai also implies OpenAI-backed usage.
    provider = (os.environ.get("AI_PROVIDER") or "").strip().lower()
    if provider == "openai":
        return "openai"
    return "ai_service"


def _check_env_present(name: str, *, as_file: bool = False) -> str | None:
    """Return an error string if the named credential is missing/placeholder; else None."""
    raw = os.environ.get(name)
    if raw is None or _is_placeholder(str(raw)):
        return f"missing or placeholder credential: {name}"
    value = str(raw).strip()
    if as_file:
        path = Path(value).expanduser()
        if not path.is_file():
            return f"credential file missing for {name}: {path}"
        if path.stat().st_size <= 0:
            return f"credential file empty for {name}: {path}"
    return None


def _enabled_channel_profiles(channels_path: Path | None) -> list[dict[str, Any]]:
    if channels_path is None:
        raw = os.environ.get("OUTPUT_FUNNEL_CHANNELS", "").strip()
        channels_path = Path(raw).expanduser() if raw else None
    if channels_path is None or not channels_path.is_file():
        return []
    try:
        payload = json.loads(channels_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    channels = payload.get("channels") if isinstance(payload, dict) else None
    if not isinstance(channels, list):
        return []
    out: list[dict[str, Any]] = []
    for item in channels:
        if isinstance(item, dict) and bool(item.get("enabled")):
            out.append(item)
    return out


def _credential_requirements_for_profile(profile: dict[str, Any]) -> list[tuple[str, bool]]:
    """Return list of (env_var_name, is_file_path) required by an enabled profile."""
    credentials = profile.get("credentials") if isinstance(profile.get("credentials"), dict) else {}
    required: list[tuple[str, bool]] = []
    file_keys = {"token_file_env", "client_secret_file_env"}
    value_keys = {
        "access_token_env",
        "token_expires_at_env",
        "page_id_env",
        "ig_user_id_env",
    }
    for key, is_file in ((k, True) for k in file_keys):
        env_name = str(credentials.get(key) or "").strip()
        if env_name:
            required.append((env_name, is_file))
    for key in value_keys:
        env_name = str(credentials.get(key) or "").strip()
        if env_name:
            # token_expires_at is optional metadata; require only access tokens / ids
            if key == "token_expires_at_env":
                continue
            required.append((env_name, False))
    return required


def validate_production_secrets(
    *,
    require_production_secrets: bool,
    uploading_enabled: bool,
    upload_mode: str = "dry_run",
    channels_path: Path | None = None,
    environ: dict[str, str] | None = None,
) -> SecretCheckResult:
    """
    Validate secrets conditionally.

    When require_production_secrets is false, returns ok with no checks.
    """
    if environ is not None:
        # Temporarily overlay for tests without mutating process env permanently.
        original = {k: os.environ.get(k) for k in environ}
        try:
            os.environ.update({k: v for k, v in environ.items()})
            return _validate_production_secrets_inner(
                require_production_secrets=require_production_secrets,
                uploading_enabled=uploading_enabled,
                upload_mode=upload_mode,
                channels_path=channels_path,
            )
        finally:
            for key, prior in original.items():
                if prior is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = prior

    return _validate_production_secrets_inner(
        require_production_secrets=require_production_secrets,
        uploading_enabled=uploading_enabled,
        upload_mode=upload_mode,
        channels_path=channels_path,
    )


def _validate_production_secrets_inner(
    *,
    require_production_secrets: bool,
    uploading_enabled: bool,
    upload_mode: str,
    channels_path: Path | None,
) -> SecretCheckResult:
    result = SecretCheckResult(ok=True)
    if not require_production_secrets:
        return result

    backend = resolve_clip_selection_backend_for_secrets()
    if backend == "openai":
        result.required_names.append("OPENAI_API_KEY")
        err = _check_env_present("OPENAI_API_KEY", as_file=False)
        if err:
            result.ok = False
            result.errors.append(err)
    else:
        result.warnings.append(
            "local AI backend active; OPENAI_API_KEY not required"
        )

    mode = (upload_mode or "dry_run").strip().lower()
    real_upload = uploading_enabled and mode == "real"
    if not real_upload:
        result.warnings.append(
            "platform posting credentials not required "
            f"(uploading_enabled={uploading_enabled}, upload_mode={mode})"
        )
        return result

    profiles = _enabled_channel_profiles(channels_path)
    if not profiles:
        result.ok = False
        result.errors.append(
            "real uploading enabled but no enabled channel profiles found "
            "to validate credentials against"
        )
        return result

    for profile in profiles:
        for env_name, as_file in _credential_requirements_for_profile(profile):
            if env_name not in result.required_names:
                result.required_names.append(env_name)
            err = _check_env_present(env_name, as_file=as_file)
            if err:
                result.ok = False
                result.errors.append(
                    f"channel {profile.get('channel_id')!r}: {err}"
                )
    return result


def validate_from_resolved_config(
    resolved: Any,
    *,
    upload_mode: str | None = None,
    channels_path: Path | None = None,
) -> SecretCheckResult:
    """Validate using a ConfigManager ResolvedConfig-like object."""
    require = bool(resolved.get("runtime.require_production_secrets"))
    uploading = bool(getattr(resolved, "uploading_enabled", False))
    mode = (upload_mode if upload_mode is not None else os.environ.get("MK04_UPLOAD_MODE", "dry_run"))
    return validate_production_secrets(
        require_production_secrets=require,
        uploading_enabled=uploading,
        upload_mode=str(mode or "dry_run"),
        channels_path=channels_path,
    )
