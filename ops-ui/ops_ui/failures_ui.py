"""Failures page UI context from the observability failures layer."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .config import Settings
from .shell import _mk04_env_token, build_shell_context

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from observability.failures import (  # noqa: E402
    failure_group_payload,
    failures_payload,
)


def build_failures_list_context(
    settings: Settings,
    *,
    shell: dict[str, Any] | None = None,
) -> dict[str, Any]:
    shell_ctx = shell if shell is not None else build_shell_context(settings)
    token = str(shell_ctx.get("shell_env_token") or _mk04_env_token(settings))
    connected = bool(shell_ctx.get("shell_connected"))

    payload: dict[str, Any] | None = None
    failures_error: str | None = None
    if connected:
        try:
            payload = failures_payload(token)
        except Exception as exc:
            failures_error = exc.__class__.__name__

    groups = []
    if payload:
        groups = [g for g in (payload.get("groups") or []) if isinstance(g, dict)]

    return {
        **shell_ctx,
        "failures_connected": connected,
        "failures_error": failures_error,
        "failures_total": int((payload or {}).get("total_failures") or 0),
        "failures_jobs": int((payload or {}).get("failed_jobs") or 0),
        "failures_runs": int((payload or {}).get("failed_runs") or 0),
        "failures_groups_count": int((payload or {}).get("distinct_groups") or 0),
        "failure_groups": groups,
        "failures_empty": connected and not groups and not failures_error,
        "failures_loop_links": [
            {"label": "Operator Console", "href": "/ops", "primary": True},
            {"label": "Runs", "href": "/ops/runs"},
            {"label": "Jobs (failed)", "href": "/ops/jobs?state=failed"},
            {"label": "Configuration", "href": "/ops/configuration"},
            {"label": "Storage", "href": "/ops/storage"},
        ],
    }


def build_failure_group_context(
    settings: Settings,
    group_key: str,
    *,
    shell: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    shell_ctx = shell if shell is not None else build_shell_context(settings)
    token = str(shell_ctx.get("shell_env_token") or _mk04_env_token(settings))
    try:
        payload = failure_group_payload(token, group_key)
    except Exception:
        return None
    if payload is None:
        return None
    return {
        **shell_ctx,
        "failure_group": payload.get("group") or {},
        "related_jobs": payload.get("related_jobs") or [],
        "related_runs": payload.get("related_runs") or [],
        "group_key": group_key,
        "failures_loop_links": [
            {"label": "Operator Console", "href": "/ops", "primary": True},
            {"label": "All failures", "href": "/ops/failures"},
            {"label": "Jobs (failed)", "href": "/ops/jobs?state=failed"},
            {"label": "Configuration", "href": "/ops/configuration"},
        ],
    }
