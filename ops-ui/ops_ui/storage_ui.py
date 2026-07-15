"""Storage View UI context (Storage Phase 11).

Read-only presentation over existing storage modules and reports.
Does not implement retention, rotation, backup, or disk-pressure logic.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .shell import _mk04_env_token, build_shell_context

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
_SCRIPTS_CONFIG = _SCRIPTS_DIR / "config"
for _path in (_SCRIPTS_CONFIG, _SCRIPTS_DIR):
    text = str(_path)
    if text not in sys.path:
        sys.path.insert(0, text)

from config_manager import ConfigError, ConfigManager  # noqa: E402
from state_paths import EnvironmentStatePaths  # noqa: E402
from storage.database_backup import (  # noqa: E402
    load_latest_backup_record,
    resolve_backup_dir,
)
from storage.disk_pressure import (  # noqa: E402
    DiskPressureLevel,
    evaluate_disk_pressure,
    format_health_detail,
)
from storage.log_rotation import load_latest_rotation_record  # noqa: E402
from storage.retention_report import (  # noqa: E402
    LATEST_POINTER_NAME,
    load_latest_retention_report,
)
from storage.retention_schedule import load_latest_scheduled_retention  # noqa: E402

_ARTIFACT_KINDS = frozenset(
    {
        "retention_report",
        "scheduled_retention",
        "database_backup_record",
        "database_backup_manifest",
        "log_rotation",
    }
)


def _parse_iso(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _age_label(raw: str | None, *, now: datetime | None = None) -> str | None:
    moment = _parse_iso(raw)
    if moment is None:
        return None
    current = now or datetime.now(UTC)
    delta = current - moment
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _tone_for_status(value: str | None) -> str:
    text = (value or "").strip().upper()
    if text in {"PASS", "SUCCESS", "NORMAL", "OK"}:
        return "ok"
    if text in {"WARN", "WARNING", "URGENT", "SKIPPED", "PARTIAL"}:
        return "warn"
    if text in {"FAIL", "FAILED", "CRITICAL", "REJECT_NEW_JOBS"}:
        return "bad"
    return "muted"


def _format_bytes(num: int | None) -> str:
    if num is None:
        return "unknown"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num} B"


def _load_resolved(token: str):
    return ConfigManager.load(
        environment=token,
        funnel_id="business",
        platform_id="youtube",
    )


def _storage_records_dir(resolved) -> Path:
    state = EnvironmentStatePaths.from_resolved_config(resolved)
    return state.data_root / "storage"


def _retention_report_dir(resolved) -> Path:
    state = EnvironmentStatePaths.from_resolved_config(resolved)
    return state.reports_root / "retention"


def _disk_section(resolved, health_data: dict[str, Any] | None) -> dict[str, Any]:
    try:
        status = evaluate_disk_pressure(resolved)
    except Exception as exc:
        return {
            "available": False,
            "error": exc.__class__.__name__,
            "detail": str(exc),
            "tone": "muted",
        }

    snapshot = status.snapshot
    health_disk = health_data.get("disk") if isinstance(health_data, dict) else None
    usage = snapshot.usage_percent if snapshot else None
    if usage is None and isinstance(health_disk, dict):
        usage = health_disk.get("usage_percent")

    return {
        "available": snapshot is not None,
        "level": status.level.value,
        "tone": _tone_for_status(status.level.value),
        "usage_percent": usage,
        "total_bytes": snapshot.total_bytes if snapshot else None,
        "used_bytes": snapshot.used_bytes if snapshot else None,
        "free_bytes": snapshot.free_bytes if snapshot else None,
        "total_label": _format_bytes(snapshot.total_bytes if snapshot else None),
        "used_label": _format_bytes(snapshot.used_bytes if snapshot else None),
        "free_label": _format_bytes(snapshot.free_bytes if snapshot else None),
        "path": snapshot.path if snapshot else None,
        "retention_recommended": status.retention_recommended,
        "detail": format_health_detail(status),
        "thresholds": status.thresholds.to_dict(),
        "error": status.error,
        "health_status": (
            str(health_disk.get("status") or "").upper()
            if isinstance(health_disk, dict)
            else None
        ),
    }


def _paths_section(resolved) -> dict[str, Any]:
    state = EnvironmentStatePaths.from_resolved_config(resolved)
    token = "prod" if resolved.environment == "production" else "dev"
    backup_dir = resolve_backup_dir(resolved)
    return {
        "data_root": str(state.data_root),
        "jobs_root": str(state.jobs_root),
        "logs_root": str(state.logs_root),
        "reports_root": str(state.reports_root),
        "database_path": str(state.database_path),
        "backups_root": str(Path(resolved._repo_root) / "backups" / token),
        "database_backup_dir": str(backup_dir),
        "retention_reports_dir": str(state.reports_root / "retention"),
        "storage_records_dir": str(state.data_root / "storage"),
    }


def _retention_section(resolved) -> dict[str, Any]:
    report_dir = _retention_report_dir(resolved)
    records_dir = _storage_records_dir(resolved)
    report = load_latest_retention_report(report_dir)
    scheduled = load_latest_scheduled_retention(records_dir=records_dir)

    report_path = None
    pointer = report_dir / LATEST_POINTER_NAME
    if pointer.is_file():
        try:
            import json

            payload = json.loads(pointer.read_text(encoding="utf-8"))
            name = payload.get("report_path")
            if isinstance(name, str) and name.strip():
                candidate = report_dir / name
                if candidate.is_file():
                    report_path = str(candidate)
        except (OSError, ValueError):
            report_path = None

    if report is None and scheduled is None:
        return {
            "available": False,
            "tone": "muted",
            "status": "none",
            "detail": "No retention reports yet",
        }

    # Prefer scheduled execution record for operator status; fall back to report.
    status = None
    timestamp = None
    mode = None
    reason = None
    if isinstance(scheduled, dict):
        status = str(scheduled.get("status") or "unknown")
        timestamp = scheduled.get("timestamp")
        mode = scheduled.get("mode")
        reason = scheduled.get("reason") or scheduled.get("detail")
    if report is not None:
        mode = mode or report.get("mode")
        timestamp = timestamp or report.get("finished_at") or report.get("started_at")
        if status is None:
            status = "SUCCESS"

    files_considered = report.get("files_considered") if report else None
    files_deleted = report.get("files_deleted") if report else None
    if files_deleted is None and report is not None:
        # Apply reports use files_deleted; dry-run uses eligible count.
        files_deleted = report.get("eligible_count")
        if files_deleted is None and isinstance(report.get("eligible_files"), list):
            files_deleted = len(report["eligible_files"])
    bytes_reclaimed = None
    if report is not None:
        bytes_reclaimed = report.get("bytes_deleted")
        if bytes_reclaimed is None:
            bytes_reclaimed = report.get("bytes_reclaimable")

    return {
        "available": True,
        "tone": _tone_for_status(status),
        "status": status or "unknown",
        "mode": mode,
        "timestamp": timestamp,
        "age_label": _age_label(str(timestamp) if timestamp else None),
        "files_considered": files_considered,
        "files_deleted": files_deleted,
        "bytes_reclaimed": bytes_reclaimed,
        "bytes_reclaimed_label": _format_bytes(
            int(bytes_reclaimed) if isinstance(bytes_reclaimed, (int, float)) else None
        ),
        "reason": reason,
        "report_path": report_path,
        "scheduled_record_path": str(records_dir / "scheduled_retention_latest.json")
        if (records_dir / "scheduled_retention_latest.json").is_file()
        else None,
        "report_mode": report.get("mode") if report else None,
        "retention_run_id": report.get("retention_run_id") if report else None,
    }


def _backup_section(resolved) -> dict[str, Any]:
    records_dir = _storage_records_dir(resolved)
    record = load_latest_backup_record(records_dir=records_dir)
    if not record:
        return {
            "available": False,
            "tone": "muted",
            "status": "none",
            "detail": "No database backup records yet",
        }
    status = str(record.get("status") or "unknown")
    size = record.get("backup_size_bytes")
    return {
        "available": True,
        "tone": _tone_for_status(status),
        "status": status,
        "timestamp": record.get("timestamp"),
        "age_label": _age_label(str(record.get("timestamp") or "")),
        "integrity_ok": record.get("integrity_ok"),
        "backup_size_bytes": size,
        "backup_size_label": _format_bytes(int(size) if isinstance(size, (int, float)) else None),
        "backup_path": record.get("backup_path"),
        "manifest_path": record.get("manifest_path"),
        "database_path": record.get("database_path"),
        "backup_count": record.get("backup_count"),
        "reason": record.get("reason") or record.get("detail"),
        "record_path": str(records_dir / "database_backup_latest.json"),
    }


def _log_rotation_section(resolved) -> dict[str, Any]:
    records_dir = _storage_records_dir(resolved)
    record = load_latest_rotation_record(records_dir=records_dir)
    if not record:
        return {
            "available": False,
            "tone": "muted",
            "status": "none",
            "detail": "No log rotation records yet",
        }
    status = str(record.get("status") or "unknown")
    return {
        "available": True,
        "tone": _tone_for_status(status),
        "status": status,
        "timestamp": record.get("timestamp"),
        "age_label": _age_label(str(record.get("timestamp") or "")),
        "rotated_count": record.get("rotated_count"),
        "active_log_count": record.get("active_log_count"),
        "failure_count": record.get("failure_count"),
        "reason": record.get("reason") or record.get("detail"),
        "record_path": str(records_dir / "log_rotation_latest.json"),
    }


def _build_warnings(
    *,
    disk: dict[str, Any],
    retention: dict[str, Any],
    backup: dict[str, Any],
    resolved,
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    level = str(disk.get("level") or "")
    if level in {
        DiskPressureLevel.WARNING.value,
        DiskPressureLevel.URGENT.value,
        DiskPressureLevel.CRITICAL.value,
        DiskPressureLevel.REJECT_NEW_JOBS.value,
    }:
        warnings.append(
            {
                "tone": "bad" if level in {"CRITICAL", "REJECT_NEW_JOBS"} else "warn",
                "title": f"Disk pressure: {level}",
                "detail": str(disk.get("detail") or "Storage usage exceeds configured threshold."),
            }
        )

    if retention.get("available") and str(retention.get("status") or "").upper() == "FAIL":
        warnings.append(
            {
                "tone": "bad",
                "title": "Latest retention failed",
                "detail": str(retention.get("reason") or "Scheduled retention reported FAIL."),
            }
        )
    elif not retention.get("available"):
        warnings.append(
            {
                "tone": "warn",
                "title": "Retention has not run",
                "detail": "No retention report or scheduled retention record is available yet.",
            }
        )
    else:
        stamp = _parse_iso(str(retention.get("timestamp") or ""))
        if stamp is not None and datetime.now(UTC) - stamp > timedelta(days=2):
            warnings.append(
                {
                    "tone": "warn",
                    "title": "Retention has not run recently",
                    "detail": f"Last retention activity was {retention.get('age_label') or 'more than 2 days ago'}.",
                }
            )

    if backup.get("available") and str(backup.get("status") or "").upper() == "FAIL":
        warnings.append(
            {
                "tone": "bad",
                "title": "Latest database backup failed",
                "detail": str(backup.get("reason") or "Database backup reported FAIL."),
            }
        )
    elif not backup.get("available"):
        warnings.append(
            {
                "tone": "warn",
                "title": "No successful database backup recorded",
                "detail": "No database_backup_latest.json record is available yet.",
            }
        )
    else:
        stamp = _parse_iso(str(backup.get("timestamp") or ""))
        retention_days = resolved.get("storage.retention.database_backups_days")
        max_age_days = 2
        if isinstance(retention_days, int) and not isinstance(retention_days, bool):
            max_age_days = max(1, min(retention_days, 7))
        if stamp is not None and datetime.now(UTC) - stamp > timedelta(days=max_age_days):
            warnings.append(
                {
                    "tone": "warn",
                    "title": "No recent successful database backup",
                    "detail": f"Last backup activity was {backup.get('age_label') or 'stale'}.",
                }
            )
        if backup.get("integrity_ok") is False:
            warnings.append(
                {
                    "tone": "bad",
                    "title": "Backup integrity check failed",
                    "detail": str(backup.get("reason") or "Latest backup failed integrity verification."),
                }
            )

    return warnings


def _overall_health(disk: dict[str, Any], warnings: list[dict[str, str]]) -> dict[str, str]:
    if any(item.get("tone") == "bad" for item in warnings):
        return {"label": "ATTENTION", "tone": "bad"}
    if any(item.get("tone") == "warn" for item in warnings):
        return {"label": "WARN", "tone": "warn"}
    level = str(disk.get("level") or "")
    if level == DiskPressureLevel.NORMAL.value and disk.get("available"):
        return {"label": "HEALTHY", "tone": "ok"}
    if disk.get("available"):
        return {"label": level or "UNKNOWN", "tone": _tone_for_status(level)}
    return {"label": "UNKNOWN", "tone": "muted"}


def build_storage_context(
    settings: Settings,
    *,
    shell: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build read-only Storage View template context."""
    shell_ctx = shell if shell is not None else build_shell_context(settings)
    token = str(shell_ctx.get("shell_env_token") or _mk04_env_token(settings))
    health_data = shell_ctx.get("shell_health_data")

    try:
        resolved = _load_resolved(token)
        load_error = None
    except (ConfigError, Exception) as exc:
        return {
            **shell_ctx,
            "storage_error": exc.__class__.__name__,
            "storage_error_detail": str(exc),
            "storage_overall": {"label": "UNAVAILABLE", "tone": "bad"},
            "storage_disk": {"available": False},
            "storage_paths": {},
            "storage_retention": {"available": False},
            "storage_backup": {"available": False},
            "storage_log_rotation": {"available": False},
            "storage_warnings": [
                {
                    "tone": "bad",
                    "title": "Storage backend unavailable",
                    "detail": str(exc),
                }
            ],
            "storage_links": [],
        }

    disk = _disk_section(resolved, health_data if isinstance(health_data, dict) else None)
    paths = _paths_section(resolved)
    retention = _retention_section(resolved)
    backup = _backup_section(resolved)
    log_rotation = _log_rotation_section(resolved)
    warnings = _build_warnings(
        disk=disk,
        retention=retention,
        backup=backup,
        resolved=resolved,
    )
    overall = _overall_health(disk, warnings)

    links: list[dict[str, str]] = []
    if retention.get("report_path"):
        links.append(
            {
                "label": "Latest retention report",
                "kind": "retention_report",
                "path": str(retention["report_path"]),
            }
        )
    if retention.get("scheduled_record_path"):
        links.append(
            {
                "label": "Scheduled retention record",
                "kind": "scheduled_retention",
                "path": str(retention["scheduled_record_path"]),
            }
        )
    if backup.get("record_path"):
        links.append(
            {
                "label": "Latest backup record",
                "kind": "database_backup_record",
                "path": str(backup["record_path"]),
            }
        )
    if backup.get("manifest_path"):
        links.append(
            {
                "label": "Latest backup manifest",
                "kind": "database_backup_manifest",
                "path": str(backup["manifest_path"]),
            }
        )
    if log_rotation.get("record_path"):
        links.append(
            {
                "label": "Latest log rotation record",
                "kind": "log_rotation",
                "path": str(log_rotation["record_path"]),
            }
        )

    return {
        **shell_ctx,
        "storage_error": load_error,
        "storage_error_detail": None,
        "storage_overall": overall,
        "storage_disk": disk,
        "storage_paths": paths,
        "storage_retention": retention,
        "storage_backup": backup,
        "storage_log_rotation": log_rotation,
        "storage_warnings": warnings,
        "storage_links": links,
    }


def resolve_storage_artifact(settings: Settings, kind: str) -> Path | None:
    """Resolve an allowlisted storage artifact path for download.

    Only known operational records/reports are served — not arbitrary paths.
    """
    if kind not in _ARTIFACT_KINDS:
        return None
    token = _mk04_env_token(settings)
    try:
        resolved = _load_resolved(token)
    except Exception:
        return None

    records_dir = _storage_records_dir(resolved)
    report_dir = _retention_report_dir(resolved)

    if kind == "scheduled_retention":
        path = records_dir / "scheduled_retention_latest.json"
    elif kind == "database_backup_record":
        path = records_dir / "database_backup_latest.json"
    elif kind == "log_rotation":
        path = records_dir / "log_rotation_latest.json"
    elif kind == "retention_report":
        report = load_latest_retention_report(report_dir)
        if report is None:
            return None
        pointer = report_dir / LATEST_POINTER_NAME
        try:
            import json

            payload = json.loads(pointer.read_text(encoding="utf-8"))
            name = payload.get("report_path")
            if not isinstance(name, str):
                return None
            path = report_dir / name
        except (OSError, ValueError):
            return None
    elif kind == "database_backup_manifest":
        record = load_latest_backup_record(records_dir=records_dir)
        if not record or not record.get("manifest_path"):
            return None
        path = Path(str(record["manifest_path"]))
    else:
        return None

    if not path.is_file():
        return None

    # Path must stay under environment roots.
    state = EnvironmentStatePaths.from_resolved_config(resolved)
    token_name = "prod" if resolved.environment == "production" else "dev"
    allowed = [
        state.data_root,
        state.reports_root,
        Path(resolved._repo_root) / "backups" / token_name,
    ]
    try:
        resolved_path = path.resolve()
    except OSError:
        return None
    for root in allowed:
        try:
            resolved_path.relative_to(root.resolve())
            return resolved_path
        except ValueError:
            continue
    return None
