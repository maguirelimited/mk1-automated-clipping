"""Disk pressure checks for Storage & Data Management (Phase 7).

Measures filesystem utilisation, classifies pressure using configured thresholds,
gates new production pipeline runs, and records blocked runs.

Does **not** delete files, trigger retention dry-run/apply, or run scheduled retention.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

_SCRIPTS_CONFIG = Path(__file__).resolve().parents[1] / "config"
if str(_SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_CONFIG))

from config_manager import ResolvedConfig  # noqa: E402
from state_paths import EnvironmentStatePaths  # noqa: E402

BLOCK_RECORD_SCHEMA_VERSION = 1
BLOCK_RECORD_FILENAME = "disk_pressure_blocks.jsonl"

DiskUsageFn = Callable[[Path], Any]


class DiskPressureLevel(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    URGENT = "URGENT"
    CRITICAL = "CRITICAL"
    REJECT_NEW_JOBS = "REJECT_NEW_JOBS"


@dataclass(frozen=True)
class DiskPressureThresholds:
    warning_percent: int
    urgent_percent: int
    critical_percent: int
    reject_new_jobs_percent: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class DiskUsageSnapshot:
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    usage_percent: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiskPressureStatus:
    snapshot: DiskUsageSnapshot | None
    level: DiskPressureLevel
    thresholds: DiskPressureThresholds
    retention_recommended: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
            "level": self.level.value,
            "thresholds": self.thresholds.to_dict(),
            "retention_recommended": self.retention_recommended,
            "error": self.error,
        }


@dataclass(frozen=True)
class JobStartGateResult:
    allowed: bool
    environment: str
    canonical_environment: str
    is_production: bool
    status: DiskPressureStatus
    reason: str | None = None
    detail: str | None = None

    @property
    def log_message(self) -> str:
        if self.allowed:
            usage = self._usage_text()
            return (
                f"disk pressure gate: allowed ({self.status.level.value}; {usage})"
            )
        return f"disk pressure gate: blocked — {self.reason}"

    def _usage_text(self) -> str:
        if self.status.snapshot is None:
            return "usage unavailable"
        return f"{self.status.snapshot.usage_percent:.1f}% used"


@dataclass(frozen=True)
class DiskPressureBlockRecord:
    schema_version: int
    timestamp: str
    environment: str
    usage_percent: float | None
    reject_threshold_percent: int
    pressure_level: str
    reason: str
    run_id: str | None = None
    trigger: str | None = None
    funnel_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _config_int(resolved: ResolvedConfig, key: str) -> int | None:
    value = resolved.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def load_disk_pressure_thresholds(resolved: ResolvedConfig) -> DiskPressureThresholds:
    warning = _config_int(resolved, "storage.disk_pressure.warning_percent")
    urgent = _config_int(resolved, "storage.disk_pressure.urgent_percent")
    critical = _config_int(resolved, "storage.disk_pressure.critical_percent")
    reject = _config_int(resolved, "storage.disk_pressure.reject_new_jobs_percent")
    missing = [
        name
        for name, value in (
            ("warning_percent", warning),
            ("urgent_percent", urgent),
            ("critical_percent", critical),
            ("reject_new_jobs_percent", reject),
        )
        if value is None
    ]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"storage.disk_pressure missing or invalid: {joined}")
    assert warning is not None
    assert urgent is not None
    assert critical is not None
    assert reject is not None
    return DiskPressureThresholds(
        warning_percent=warning,
        urgent_percent=urgent,
        critical_percent=critical,
        reject_new_jobs_percent=reject,
    )


def classify_disk_pressure(
    usage_percent: float,
    thresholds: DiskPressureThresholds,
) -> DiskPressureLevel:
    """Classify usage percent against configured thresholds (deterministic)."""
    if usage_percent >= thresholds.reject_new_jobs_percent:
        return DiskPressureLevel.REJECT_NEW_JOBS
    if usage_percent >= thresholds.critical_percent:
        return DiskPressureLevel.CRITICAL
    if usage_percent >= thresholds.urgent_percent:
        return DiskPressureLevel.URGENT
    if usage_percent >= thresholds.warning_percent:
        return DiskPressureLevel.WARNING
    return DiskPressureLevel.NORMAL


def retention_recommended_for_level(level: DiskPressureLevel) -> bool:
    """Retention is recommended at high pressure; execution remains operator-controlled."""
    return level in {DiskPressureLevel.CRITICAL, DiskPressureLevel.REJECT_NEW_JOBS}


def measure_disk_usage(
    path: Path,
    *,
    disk_usage_fn: DiskUsageFn | None = None,
) -> tuple[DiskUsageSnapshot | None, str | None]:
    usage_fn = disk_usage_fn or shutil.disk_usage
    try:
        usage = usage_fn(path)
    except OSError as exc:
        return None, str(exc)
    total = int(usage.total)
    if total <= 0:
        return None, "total disk size is zero"
    used = int(usage.used)
    free = int(usage.free)
    percent = round(used / total * 100, 1)
    return (
        DiskUsageSnapshot(
            path=str(path),
            total_bytes=total,
            used_bytes=used,
            free_bytes=free,
            usage_percent=percent,
        ),
        None,
    )


def _default_disk_path(resolved: ResolvedConfig) -> Path:
    state = EnvironmentStatePaths.from_resolved_config(resolved)
    for candidate in (
        state.data_root,
        state.jobs_root,
        state.outputs_root,
        state.logs_root,
    ):
        if candidate.exists():
            return candidate
    return state.data_root


def evaluate_disk_pressure(
    resolved: ResolvedConfig,
    *,
    path: Path | None = None,
    disk_usage_fn: DiskUsageFn | None = None,
) -> DiskPressureStatus:
    thresholds = load_disk_pressure_thresholds(resolved)
    target = path if path is not None else _default_disk_path(resolved)
    snapshot, error = measure_disk_usage(target, disk_usage_fn=disk_usage_fn)
    if snapshot is None:
        return DiskPressureStatus(
            snapshot=None,
            level=DiskPressureLevel.WARNING,
            thresholds=thresholds,
            retention_recommended=False,
            error=error,
        )
    level = classify_disk_pressure(snapshot.usage_percent, thresholds)
    return DiskPressureStatus(
        snapshot=snapshot,
        level=level,
        thresholds=thresholds,
        retention_recommended=retention_recommended_for_level(level),
        error=None,
    )


def format_threshold_summary(thresholds: DiskPressureThresholds) -> str:
    return (
        f"warn>={thresholds.warning_percent}% "
        f"urgent>={thresholds.urgent_percent}% "
        f"critical>={thresholds.critical_percent}% "
        f"reject>={thresholds.reject_new_jobs_percent}%"
    )


def format_health_detail(status: DiskPressureStatus) -> str:
    if status.snapshot is None:
        return status.error or "disk usage unavailable"
    threshold_note = format_threshold_summary(status.thresholds)
    retention_note = (
        "; retention recommended (operator-controlled)"
        if status.retention_recommended
        else ""
    )
    return (
        f"{status.snapshot.usage_percent:.1f}% used on {status.snapshot.path}; "
        f"storage_state={status.level.value}; thresholds {threshold_note}"
        f"{retention_note}"
    )


def health_result_for_level(level: DiskPressureLevel) -> tuple[str, str]:
    if level == DiskPressureLevel.NORMAL:
        return "PASS", "info"
    if level in {DiskPressureLevel.WARNING, DiskPressureLevel.URGENT}:
        return "WARN", "warn"
    return "FAIL", "fail"


def parse_storage_state_from_detail(detail: str | None) -> str | None:
    if not detail:
        return None
    match = re.search(r"storage_state=([A-Z_]+)", detail)
    if not match:
        return None
    return match.group(1)


def blocks_path_for_environment(
    environment: str,
    *,
    repo_root: Path,
    data_root: Path | None = None,
) -> Path:
    if data_root is not None:
        root = data_root
    else:
        root = repo_root / "data" / environment
    return root / "storage" / BLOCK_RECORD_FILENAME


def record_disk_pressure_block(
    *,
    environment: str,
    status: DiskPressureStatus,
    reason: str,
    repo_root: Path,
    data_root: Path | None = None,
    run_id: str | None = None,
    trigger: str | None = None,
    funnel_id: str | None = None,
    timestamp: str | None = None,
) -> Path:
    """Append structured evidence when production work is blocked by disk pressure."""
    usage = status.snapshot.usage_percent if status.snapshot else None
    record = DiskPressureBlockRecord(
        schema_version=BLOCK_RECORD_SCHEMA_VERSION,
        timestamp=timestamp or _utc_now_iso(),
        environment=environment,
        usage_percent=usage,
        reject_threshold_percent=status.thresholds.reject_new_jobs_percent,
        pressure_level=status.level.value,
        reason=reason,
        run_id=run_id,
        trigger=trigger,
        funnel_id=funnel_id,
    )
    path = blocks_path_for_environment(environment, repo_root=repo_root, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_dict(), sort_keys=True))
        handle.write("\n")
    return path


def _canonical_environment_token(environment: str) -> tuple[str, str, bool]:
    token = environment.strip().lower()
    if token in {"dev", "development"}:
        return "development", "dev", False
    if token in {"prod", "production"}:
        return "production", "prod", True
    raise ValueError(f"invalid environment: {environment!r}")


def can_start_new_job(
    environment: str,
    resolved: ResolvedConfig,
    *,
    disk_status: DiskPressureStatus | None = None,
    disk_usage_fn: DiskUsageFn | None = None,
) -> JobStartGateResult:
    """Return whether a new production pipeline run may begin."""
    canonical, mk04_env, is_production = _canonical_environment_token(environment)
    status = disk_status or evaluate_disk_pressure(
        resolved,
        disk_usage_fn=disk_usage_fn,
    )
    if not is_production:
        return JobStartGateResult(
            allowed=True,
            environment=mk04_env,
            canonical_environment=canonical,
            is_production=False,
            status=status,
            reason=None,
            detail=format_health_detail(status),
        )

    if status.snapshot is None:
        reason = status.error or "disk usage unavailable"
        return JobStartGateResult(
            allowed=False,
            environment=mk04_env,
            canonical_environment=canonical,
            is_production=True,
            status=status,
            reason=reason,
            detail=reason,
        )

    if status.level != DiskPressureLevel.REJECT_NEW_JOBS:
        return JobStartGateResult(
            allowed=True,
            environment=mk04_env,
            canonical_environment=canonical,
            is_production=True,
            status=status,
            reason=None,
            detail=format_health_detail(status),
        )

    threshold = status.thresholds.reject_new_jobs_percent
    usage = status.snapshot.usage_percent
    reason = (
        f"disk usage {usage:.1f}% exceeds production reject threshold {threshold}%"
    )
    detail = (
        f"{reason}. Suggested action: review retention dry-run/apply (operator-controlled)."
    )
    return JobStartGateResult(
        allowed=False,
        environment=mk04_env,
        canonical_environment=canonical,
        is_production=True,
        status=status,
        reason=reason,
        detail=detail,
    )
