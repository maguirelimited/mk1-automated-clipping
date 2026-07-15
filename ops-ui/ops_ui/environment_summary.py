"""Read-only environment and config summary for the Operations UI."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from .config import BASE_DIR, Settings

_SECRET_KEY_RE = re.compile(
    r"(password|secret|token|api[_-]?key|cookie|credential|private[_-]?key|auth)",
    re.IGNORECASE,
)

_ENV_LABELS = {
    "development": "DEVELOPMENT",
    "production": "PRODUCTION",
}


def _ensure_config_scripts_on_path() -> Path:
    scripts_config = BASE_DIR / "scripts" / "config"
    scripts_config_str = str(scripts_config)
    if scripts_config_str not in sys.path:
        sys.path.insert(0, scripts_config_str)
    return scripts_config


def _ensure_ops_scripts_on_path() -> Path:
    scripts_ops = BASE_DIR / "scripts" / "ops"
    scripts_ops_str = str(scripts_ops)
    if scripts_ops_str not in sys.path:
        sys.path.insert(0, scripts_ops_str)
    return scripts_ops


def redact_dict(value: Any, *, max_depth: int = 8) -> Any:
    """Recursively redact secret-looking keys from dicts/lists for safe display."""
    if max_depth <= 0:
        return "[truncated]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_KEY_RE.search(key_text):
                out[key_text] = "[REDACTED]"
            else:
                out[key_text] = redact_dict(item, max_depth=max_depth - 1)
        return out
    if isinstance(value, list):
        return [redact_dict(item, max_depth=max_depth - 1) for item in value[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and _looks_like_secret_value(value):
            return "[REDACTED]"
        return value
    return str(value)


def _looks_like_secret_value(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if text.startswith("sk-") or text.startswith("Bearer "):
        return True
    if len(text) > 40 and all(c.isalnum() or c in "-_=" for c in text):
        return True
    return False


def normalize_mk04_env(raw: str | None) -> tuple[str | None, str | None]:
    """
    Return (canonical_environment, error_message).

    canonical_environment is "development" or "production" when valid.
    """
    _ensure_config_scripts_on_path()
    from environment_names import EnvironmentNameError, to_config_environment

    if raw is None or str(raw).strip() == "":
        return "development", None

    try:
        return to_config_environment(raw), None
    except EnvironmentNameError:
        return None, (
            f"Invalid MK04_ENV {raw!r}. Expected dev, development, prod, or production."
        )


def environment_label_for(canonical: str) -> str:
    return _ENV_LABELS.get(canonical, canonical.upper())


def load_last_update_status(data_root: Path) -> dict[str, Any] | None:
    """Load last_update_status.json written by ./update.sh (read-only)."""
    path = data_root / "last_update_status.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return redact_dict(data)


def runtime_upload_control_label(
    *,
    config_upload_enabled: bool,
    runtime_disabled: bool | None,
) -> str:
    if runtime_disabled is True:
        return "Runtime uploads: DISABLED (kill switch active)"
    if runtime_disabled is False and config_upload_enabled:
        return "Runtime uploads: enabled (config and runtime control allow posting)"
    if runtime_disabled is False:
        return "Runtime uploads: enabled (runtime kill switch off; config disables posting)"
    return "Runtime uploads: no runtime override on file"


def build_environment_summary(
    settings: Settings,
    *,
    funnel_id: str = "business",
    platform_id: str = "youtube",
) -> dict[str, Any]:
    """
    Build a read-only, secret-safe summary of the active environment and config.

    Uses ConfigManager when available. Never mutates config or filesystem state.
    """
    canonical, env_error = normalize_mk04_env(settings.environment)
    mk04_env = settings.environment

    summary: dict[str, Any] = {
        "environment": canonical or "invalid",
        "environment_label": environment_label_for(canonical) if canonical else "INVALID ENV",
        "mk04_env": mk04_env,
        "is_production": canonical == "production",
        "posting_config_enabled": False,
        "posting_config_label": "Posting config: disabled",
        "runtime_upload_control_available": True,
        "runtime_upload_control_label": "Runtime uploads: no runtime override on file",
        "funnel_id": funnel_id,
        "platform_id": platform_id,
        "preset_id": "not_available",
        "config_root": str(BASE_DIR / "config"),
        "legacy_config_root": str(settings.config_root),
        "jobs_root": "not_available",
        "outputs_root": "not_available",
        "logs_root": "not_available",
        "runs_root": "not_available",
        "database_path": "not_available",
        "database_role": "optional_placeholder",
        "control_state_file": "not_available",
        "jobs_root_warning": "",
        "config_validation_state": "unknown",
        "config_validation_message": "",
        "health_state": "unknown",
        "boot_readiness": "unknown",
        "boot_readiness_detail": "",
        "boot_components": [],
        "last_update_status": "not_available",
        "last_update_finished_at": "not_available",
        "last_update_commit": "not_available",
        "resolved_config_available": False,
        "error": env_error,
    }

    if env_error:
        summary["config_validation_state"] = "fail"
        summary["config_validation_message"] = env_error
        return summary

    try:
        _ensure_config_scripts_on_path()
        from config_manager import ConfigError, ConfigManager  # noqa: PLC0415

        resolved = ConfigManager.load(
            environment=canonical,
            funnel_id=funnel_id,
            platform_id=platform_id,
            config_root=BASE_DIR / "config",
        )
    except Exception as exc:
        summary["config_validation_state"] = "fail"
        summary["config_validation_message"] = str(exc)[:500]
        summary["error"] = str(exc)[:500]
        return summary

    posting_enabled = bool(resolved.uploading_enabled)
    summary.update(
        {
            "funnel_id": resolved.funnel_id,
            "platform_id": resolved.platform_id,
            "preset_id": resolved.preset_id,
            "config_root": str(resolved._config_root),
            "jobs_root": str(resolved.paths.jobs_root),
            "outputs_root": str(resolved.paths.outputs_root),
            "logs_root": str(resolved.paths.logs_root),
            "runs_root": str(getattr(resolved.paths, "runs_root", "not_available")),
            "database_path": str(resolved.paths.database_path),
            "database_role": "optional_placeholder",
            "control_state_file": str(
                getattr(resolved.paths, "control_state_file", resolved.paths.data_root / "control_state.json")
            ),
            "posting_config_enabled": posting_enabled,
            "posting_config_label": (
                "Posting config: enabled" if posting_enabled else "Posting config: disabled"
            ),
            "config_validation_state": "pass",
            "config_validation_message": "Config validation: PASS",
            "resolved_config_available": True,
            "selection_mode": resolved.get("selection.mode", "not_available"),
            "selection_max_clips": resolved.get("selection.max_clips", "not_available"),
        }
    )

    update_record = load_last_update_status(resolved.state_paths.data_root)
    if update_record:
        summary["last_update_status"] = str(update_record.get("status", "unknown"))
        summary["last_update_finished_at"] = update_record.get("finished_at", "not_available")
        summary["last_update_commit"] = update_record.get("commit", "not_available")
        summary["last_update_detail"] = update_record

    _ensure_ops_scripts_on_path()
    from ops_readonly import load_runtime_upload_control  # noqa: PLC0415

    try:
        from observability.index import JOBS_ROOT_FALLBACK_WARNING  # noqa: PLC0415

        if JOBS_ROOT_FALLBACK_WARNING:
            summary["jobs_root_warning"] = JOBS_ROOT_FALLBACK_WARNING
    except Exception:
        pass

    runtime_disabled, runtime_detail = load_runtime_upload_control(resolved.state_paths.data_root)
    summary["runtime_upload_control_label"] = runtime_upload_control_label(
        config_upload_enabled=posting_enabled,
        runtime_disabled=runtime_disabled,
    )
    if runtime_detail:
        summary["runtime_upload_control_detail"] = runtime_detail

    try:
        from boot_verification import build_boot_verification  # noqa: PLC0415

        boot_token = "prod" if canonical == "production" else "dev"
        boot = build_boot_verification(boot_token)
        summary["boot_readiness"] = boot.overall
        summary["boot_readiness_detail"] = (
            "required components ready"
            if boot.overall == "READY"
            else "; ".join(
                c.label for c in boot.components if c.required and c.result == "FAIL"
            )
            or "required component failed"
        )
        summary["boot_components"] = [
            {
                "label": c.label,
                "result": c.result,
                "detail": c.detail,
                "required": c.required,
            }
            for c in boot.components
        ]
        summary["health_state"] = (
            "ready" if boot.overall == "READY" else "not_ready"
        )
    except Exception as exc:
        summary["boot_readiness"] = "unknown"
        summary["boot_readiness_detail"] = str(exc)[:200]
        summary["health_state"] = "unknown"

    return summary


def banner_text(summary: dict[str, Any]) -> str:
    """Single-line banner text for the persistent environment header."""
    if summary.get("error"):
        return f"{summary.get('environment_label', 'INVALID ENV')} — config error"
    posting = "enabled" if summary.get("posting_config_enabled") else "disabled"
    return (
        f"{summary.get('environment_label', 'UNKNOWN')} — "
        f"Posting config {posting} · "
        f"{summary.get('funnel_id', '—')} / "
        f"{summary.get('platform_id', '—')} / "
        f"{summary.get('preset_id', '—')}"
    )


def load_job_execution_context(
    job_id: str,
    jobs_root: str | None,
    *,
    report_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Best-effort load of execution context for a job detail view.

    Tries report payload, then execution_context.json under jobs_root/job_id.
    Returns redacted dict or None for legacy jobs.
    """
    if isinstance(report_payload, dict):
        ctx = report_payload.get("execution_context")
        if isinstance(ctx, dict) and ctx:
            return redact_dict(ctx)

    if not jobs_root or not job_id:
        return None

    job_dir = Path(jobs_root) / job_id
    ctx_path = job_dir / "execution_context.json"
    if not ctx_path.is_file():
        report_path = job_dir / "report.json"
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = None
            if isinstance(report, dict):
                ctx = report.get("execution_context")
                if isinstance(ctx, dict) and ctx:
                    return redact_dict(ctx)
        task_path = job_dir / "task.json"
        if task_path.is_file():
            try:
                task = json.loads(task_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                task = None
            if isinstance(task, dict):
                ctx = task.get("execution_context")
                if isinstance(ctx, dict) and ctx:
                    return redact_dict(ctx)
        return None

    try:
        data = json.loads(ctx_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return redact_dict(data)
