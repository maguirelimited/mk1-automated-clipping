"""Read-only configuration view for the Configuration Viewer (Phase 12).

Uses ConfigManager and existing validation only. Backend owns secret redaction.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

from .contract import is_secret_field_name
from .schemas import CONTRACT_SCHEMA_VERSION

_OPS_DIR = Path(__file__).resolve().parent.parent / "ops"
_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
if str(_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_OPS_DIR))
if str(_CONFIG_DIR) not in sys.path:
    sys.path.insert(0, str(_CONFIG_DIR))

from ops_readonly import (  # noqa: E402
    REPO_ROOT,
    canonical_env,
    compute_effective_scheduler,
    compute_effective_upload,
    inspect_underlying_scheduler,
    load_runtime_scheduler_control,
    load_runtime_upload_control,
    mk04_env,
)
from config_manager import ConfigError, ConfigManager  # noqa: E402
from validate_config import validate_config_tree  # noqa: E402

_REDACTED = "<redacted>"
_NOT_AVAILABLE = "Not available"


def _looks_like_secret_value(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if text.startswith("sk-") or text.startswith("Bearer "):
        return True
    if "://" in text and any(token in text.lower() for token in ("@", "token=", "key=", "password=")):
        return True
    if len(text) > 40 and all(c.isalnum() or c in "-_=" for c in text):
        return True
    return False


def redact_config_value(value: Any, *, max_depth: int = 10) -> Any:
    """Recursively redact secrets. Backend-owned; templates must not decide."""
    if max_depth <= 0:
        return _REDACTED
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if is_secret_field_name(str(key)):
                out[str(key)] = _REDACTED
            else:
                out[str(key)] = redact_config_value(item, max_depth=max_depth - 1)
        return out
    if isinstance(value, list):
        return [redact_config_value(item, max_depth=max_depth - 1) for item in value[:100]]
    if isinstance(value, str):
        if _looks_like_secret_value(value):
            return _REDACTED
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _section(data: dict[str, Any] | None, *keys: str) -> Any:
    node: Any = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def build_config_view(
    mk04_env_token: str,
    *,
    funnel_id: str = "business",
    platform_id: str = "youtube",
    preset_id: str | None = None,
) -> dict[str, Any]:
    """Build a secret-safe configuration view payload."""
    token = mk04_env(canonical_env(mk04_env_token))
    canonical = "production" if token == "prod" else "development"
    config_root = REPO_ROOT / "config"

    validation = {
        "state": "unknown",
        "message": _NOT_AVAILABLE,
        "errors": [],
    }
    tree_errors = validate_config_tree(config_root)
    if tree_errors:
        validation = {
            "state": "FAIL",
            "message": "Config validation failed",
            "errors": [str(e) for e in tree_errors[:50]],
        }
    else:
        validation = {
            "state": "PASS",
            "message": "Config validation: PASS",
            "errors": [],
        }

    try:
        resolved = ConfigManager.load(
            environment=canonical,
            funnel_id=funnel_id,
            platform_id=platform_id,
            preset_id=preset_id,
            config_root=config_root,
        )
    except ConfigError as exc:
        return {
            "environment": token,
            "environment_label": "PRODUCTION" if token == "prod" else "DEVELOPMENT",
            "validation": {
                "state": "FAIL",
                "message": str(exc)[:500],
                "errors": [str(exc)[:500]],
            },
            "summary": {
                "funnel_id": funnel_id,
                "platform_id": platform_id,
                "preset_id": preset_id or _NOT_AVAILABLE,
                "uploading_enabled": None,
            },
            "upload": {"enabled": None, "status": "unknown", "detail": _NOT_AVAILABLE},
            "scheduler": {"effective": "unknown", "status": "unknown", "detail": _NOT_AVAILABLE},
            "system": {},
            "retention": {},
            "ai": {},
            "funnel": {},
            "platform": {},
            "preset": {},
            "paths": {},
            "resolved_config": {},
            "resolved_config_available": False,
            "schema_version": CONTRACT_SCHEMA_VERSION,
        }

    # Load success implies tree validation passed for this selection.
    if validation["state"] != "FAIL":
        validation = {
            "state": "PASS",
            "message": "ConfigManager load succeeded",
            "errors": [],
        }

    data = resolved.to_dict()
    redacted = redact_config_value(data)

    upload_enabled = bool(resolved.uploading_enabled)
    runtime_disabled, runtime_detail = load_runtime_upload_control(
        resolved.state_paths.data_root
    )
    can_upload, upload_detail = compute_effective_upload(upload_enabled, runtime_disabled)
    upload = {
        "config_enabled": upload_enabled,
        "runtime_disabled": runtime_disabled,
        "enabled": can_upload,
        "status": "enabled" if can_upload is True else ("disabled" if can_upload is False else "unknown"),
        "detail": runtime_detail or upload_detail or _NOT_AVAILABLE,
    }

    sched_disabled, sched_detail = load_runtime_scheduler_control(
        resolved.state_paths.data_root
    )
    underlying = inspect_underlying_scheduler(token, REPO_ROOT)
    effective, effective_detail = compute_effective_scheduler(
        sched_disabled,
        underlying,
        mk04_env_token=token,
    )
    scheduler = {
        "effective": effective,
        "runtime_disabled": sched_disabled,
        "underlying_active": underlying.active,
        "mechanism": underlying.mechanism,
        "status": effective,
        "detail": effective_detail or sched_detail or underlying.detail or _NOT_AVAILABLE,
    }

    paths = {
        "config_root": "config",
        "jobs_root": f"jobs/{token}",
        "outputs_root": f"outputs/{token}",
        "logs_root": f"logs/{token}",
        "data_root": f"data/{token}",
        "reports_root": f"reports/{token}",
    }

    return {
        "environment": token,
        "environment_label": "PRODUCTION" if token == "prod" else "DEVELOPMENT",
        "validation": validation,
        "summary": {
            "funnel_id": resolved.funnel_id,
            "platform_id": resolved.platform_id,
            "preset_id": resolved.preset_id,
            "uploading_enabled": upload_enabled,
        },
        "upload": upload,
        "scheduler": scheduler,
        "system": redact_config_value(_section(redacted, "system") or {}),
        "retention": redact_config_value(_section(redacted, "storage", "retention") or {}),
        "disk_pressure": redact_config_value(
            _section(redacted, "storage", "disk_pressure") or {}
        ),
        "ai": redact_config_value(_section(redacted, "ai") or {}),
        "funnel": redact_config_value(_section(redacted, "funnel") or {}),
        "platform": {
            "platform_id": resolved.platform_id,
            "uploading": redact_config_value(_section(redacted, "uploading") or {}),
            "captions": redact_config_value(_section(redacted, "captions") or {}),
            "format": redact_config_value(_section(redacted, "format") or {}),
        },
        "preset": {
            "preset_id": resolved.preset_id,
            "selection": redact_config_value(_section(redacted, "selection") or {}),
            "post_processing": redact_config_value(
                _section(redacted, "post_processing") or {}
            ),
        },
        "paths": paths,
        "resolved_config": redacted,
        "resolved_config_available": True,
        "schema_version": CONTRACT_SCHEMA_VERSION,
    }
