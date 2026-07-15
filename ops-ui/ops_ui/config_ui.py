"""Configuration Viewer UI context (Phase 12). Read-only."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .config import Settings
from .shell import _mk04_env_token, build_shell_context

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from observability.config_view import build_config_view  # noqa: E402


def build_configuration_context(
    settings: Settings,
    *,
    shell: dict[str, Any] | None = None,
    funnel_id: str = "business",
    platform_id: str = "youtube",
) -> dict[str, Any]:
    shell_ctx = shell if shell is not None else build_shell_context(settings)
    token = str(shell_ctx.get("shell_env_token") or _mk04_env_token(settings))

    try:
        config_view = build_config_view(
            token,
            funnel_id=funnel_id,
            platform_id=platform_id,
        )
        config_error = None
    except Exception as exc:
        config_view = None
        config_error = exc.__class__.__name__

    resolved_json = ""
    if config_view and config_view.get("resolved_config"):
        resolved_json = json.dumps(config_view["resolved_config"], indent=2, default=str)

    return {
        **shell_ctx,
        "config_view": config_view,
        "config_error": config_error,
        "config_resolved_json": resolved_json,
        "config_funnel_id": funnel_id,
        "config_platform_id": platform_id,
    }
