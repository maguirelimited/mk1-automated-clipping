"""Operations UI shell context from the observability backend.

Read-only. Consumes the same data as GET /health and GET /status
(build_system_health / build_system_status). Does not invent health logic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .config import Settings

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from observability.populate import (  # noqa: E402
    build_system_health,
    build_system_status,
)

# Daily operator navigation (primary) and diagnostic pages (secondary).
SHELL_NAV_PRIMARY = (
    ("ops_overview", "Console", "/ops"),
    ("ops_outputs", "Outputs", "/ops/outputs"),
    ("ops_runs", "Runs", "/ops/runs"),
    ("ops_jobs", "Jobs", "/ops/jobs"),
    ("ops_failures", "Failures", "/ops/failures"),
)

SHELL_NAV_SECONDARY = (
    ("ops_storage", "Storage", "/ops/storage"),
    ("ops_configuration", "Configuration", "/ops/configuration"),
    ("health", "Health", "/health"),
    ("logs", "Logs", "/logs"),
)

# Advanced / legacy tools — outside the daily /ops operator workflow.
# Canonical pairs (daily → legacy/diagnostic):
#   /ops/failures → /failed (Legacy failed jobs)
#   /ops/jobs?state=failed → /failed
#   /ops/outputs → canonical run review (legacy /clip-review redirects here)
#   /ops → /dashboard (Mission Control)
#   /ops/configuration → /settings (Legacy settings; editable)
#   /ops/failures → /health (service doctor aggregate)
#   Job logs in inspector → /logs (service journal search)
SHELL_NAV_LEGACY = (
    ("dashboard", "Mission Control", "/dashboard"),
    ("funnels_page", "Funnels", "/funnels"),
    ("publishing", "Publishing", "/publishing"),
    ("failed_jobs", "Legacy failed jobs", "/failed"),
    ("recovery", "Recovery", "/recovery"),
    ("settings_page", "Legacy settings", "/settings"),
)

SHELL_NAV_TITLES: dict[str, str] = {
    "/ops": "Daily operator console",
    "/ops/outputs": "Inspect produced clips",
    "/ops/runs": "Pipeline run history",
    "/ops/jobs": "Work units to inspect",
    "/ops/failures": "Aggregated failures (daily path)",
    "/ops/storage": "Storage diagnostic",
    "/ops/configuration": "Read-only configuration view",
    "/health": "Service doctor checks (diagnostic)",
    "/logs": "Service journal search (diagnostic)",
    "/dashboard": "Legacy dashboard — use Console for daily ops",
    "/failed": "Legacy failure table — use Failures for daily diagnosis",
    "/settings": "Editable service settings — use Configuration for read-only config",
}

# Combined list for templates/tests that expect a flat daily + diagnostic nav.
SHELL_NAV = SHELL_NAV_PRIMARY + SHELL_NAV_SECONDARY


def _nav_items(
    entries: tuple[tuple[str, str, str], ...],
) -> list[dict[str, str]]:
    return [
        {
            "endpoint": endpoint,
            "label": label,
            "path": path,
            "title": SHELL_NAV_TITLES.get(path, ""),
        }
        for endpoint, label, path in entries
    ]


def _mk04_env_token(settings: Settings) -> str:
    env = (settings.environment or "dev").strip().lower()
    if env in {"production", "prod"}:
        return "prod"
    return "dev"


def _environment_display(token: str | None) -> tuple[str, str, bool]:
    """Return (label, css_key, is_production)."""
    value = (token or "").strip().lower()
    if value in {"prod", "production"}:
        return "PRODUCTION", "production", True
    if value in {"dev", "development"}:
        return "DEVELOPMENT", "development", False
    return "UNKNOWN", "unknown", False


def _upload_label(upload: dict[str, Any] | None) -> str:
    if not upload:
        return "unknown"
    enabled = upload.get("enabled")
    if enabled is True:
        return "enabled"
    if enabled is False:
        return "disabled"
    status = str(upload.get("status") or "").strip().lower()
    if status in {"pass", "enabled"}:
        return "enabled"
    if status in {"fail", "disabled", "warn"} and enabled is False:
        return "disabled"
    if status:
        return status
    return "unknown"


def _scheduler_label(scheduler: dict[str, Any] | None) -> str:
    if not scheduler:
        return "unknown"
    effective = str(scheduler.get("effective") or "").strip().lower()
    status = str(scheduler.get("status") or "").strip().lower()
    if effective and effective != "unknown":
        return effective
    if status and status != "unknown":
        return status
    return "unknown"


def _health_badge(overall: str | None, *, connected: bool) -> dict[str, str]:
    if not connected:
        return {"label": "DISCONNECTED", "tone": "bad"}
    value = (overall or "WARN").strip().upper()
    if value == "PASS":
        return {"label": "HEALTHY", "tone": "ok"}
    if value == "FAIL":
        return {"label": "FAIL", "tone": "bad"}
    if value == "WARN":
        return {"label": "WARN", "tone": "warn"}
    return {"label": value or "UNKNOWN", "tone": "muted"}


def build_shell_context(settings: Settings) -> dict[str, Any]:
    """Build template context for the Operations UI shell."""
    token = _mk04_env_token(settings)
    health_data: dict[str, Any] | None = None
    status_data: dict[str, Any] | None = None
    health_error: str | None = None
    status_error: str | None = None

    try:
        health_data = build_system_health(token).to_dict()
    except Exception as exc:
        health_error = exc.__class__.__name__

    try:
        status_data = build_system_status(token).to_dict()
    except Exception as exc:
        status_error = exc.__class__.__name__

    connected = health_data is not None and status_data is not None
    env_token = None
    if health_data is not None:
        env_token = health_data.get("environment")
    elif status_data is not None:
        env_token = status_data.get("environment")
    else:
        env_token = token

    env_label, env_css, is_production = _environment_display(str(env_token or token))

    overall = None if health_data is None else health_data.get("overall")
    activity = None if status_data is None else status_data.get("state")
    upload = None if health_data is None else health_data.get("upload")
    scheduler = None if health_data is None else health_data.get("scheduler")

    if not connected:
        banner_main = f"{env_label} — OBSERVABILITY BACKEND DISCONNECTED"
        banner_sub = "Health/status unavailable. Values are not fabricated."
        if health_error or status_error:
            parts = []
            if health_error:
                parts.append(f"health: {health_error}")
            if status_error:
                parts.append(f"status: {status_error}")
            banner_sub = "; ".join(parts)
    else:
        banner_main = env_label
        banner_sub = (
            f"Health {overall or 'unknown'} · "
            f"Activity {activity or 'unknown'} · "
            f"Upload {_upload_label(upload if isinstance(upload, dict) else None)} · "
            f"Scheduler {_scheduler_label(scheduler if isinstance(scheduler, dict) else None)}"
        )

    return {
        "shell_connected": connected,
        "shell_environment_label": env_label,
        "shell_environment_css": env_css,
        "shell_is_production": is_production,
        "shell_banner_main": banner_main,
        "shell_banner_sub": banner_sub,
        "shell_health_badge": _health_badge(
            str(overall) if overall is not None else None,
            connected=connected,
        ),
        "shell_activity": (activity or "unknown") if connected else "unknown",
        "shell_upload": _upload_label(upload if isinstance(upload, dict) else None)
        if connected
        else "unknown",
        "shell_scheduler": _scheduler_label(scheduler if isinstance(scheduler, dict) else None)
        if connected
        else "unknown",
        "shell_overall": (overall or "unknown") if connected else "unknown",
        # Shared backend payloads for pages (same as GET /health and GET /status).
        "shell_health_data": health_data,
        "shell_status_data": status_data,
        "shell_env_token": str(env_token or token),
        "shell_nav_primary": _nav_items(SHELL_NAV_PRIMARY),
        "shell_nav_secondary": _nav_items(SHELL_NAV_SECONDARY),
        "shell_nav_legacy": _nav_items(SHELL_NAV_LEGACY),
        "shell_nav": _nav_items(SHELL_NAV),
        "shell_health_error": health_error,
        "shell_status_error": status_error,
    }
