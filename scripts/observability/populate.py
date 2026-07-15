"""Populate observability contract models from existing ops infrastructure.

Calls scripts/ops health, status, execution-lock, and control helpers.
Does not invent new health calculations or redesign the health layer.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    ActiveRunRef,
    DiskState,
    ExecutionGateSummary,
    ExecutionLockSummary,
    QueueSummary,
    RecentActivitySummary,
    SchedulerStateSummary,
    ServiceStatus,
    SystemHealth,
    SystemStatus,
    UploadStateSummary,
)
from .schemas import CONTRACT_SCHEMA_VERSION

_OPS_DIR = Path(__file__).resolve().parent.parent / "ops"
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_OPS_DIR))
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from execution_lock import inspect_execution_lock  # noqa: E402
from health_report import HealthCheck, HealthReport, build_health_report  # noqa: E402
from ops_readonly import (  # noqa: E402
    DEFAULT_SCHEDULER_MODE,
    REPO_ROOT,
    canonical_env,
    compute_effective_scheduler,
    compute_effective_upload,
    discover_service_units,
    ensure_config_scripts_on_path,
    inspect_underlying_scheduler,
    load_runtime_scheduler_control,
    load_runtime_upload_control,
    mk04_env,
    systemd_unit_status,
)
from run_records import latest_run_record  # noqa: E402
from status_report import StatusReport, build_status_report  # noqa: E402
from storage.disk_pressure import (  # noqa: E402
    DiskPressureLevel,
    parse_storage_state_from_detail,
    retention_recommended_for_level,
)

ensure_config_scripts_on_path()
from config_manager import ConfigError, ConfigManager  # noqa: E402
from state_paths import EnvironmentStatePaths  # noqa: E402

_LABEL_TO_SERVICE_NAME = {
    "API": "api",
    "Worker": "worker",
    "AI service": "ai_service",
    "Operations UI": "operations_ui",
    "Output funnel": "output_funnel",
}

_ABS_PATH_RE = re.compile(r"(?:/[\w.-]+){2,}")
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(password|secret|token|api[_-]?key|bearer)\b\s*[:=]\s*\S+"
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitize_detail(text: str | None) -> str | None:
    """Remove secrets and absolute filesystem paths from operator-facing detail."""
    if text is None:
        return None
    cleaned = str(text).strip()
    if not cleaned:
        return None
    cleaned = _SECRET_VALUE_RE.sub(r"\1=[REDACTED]", cleaned)
    cleaned = _ABS_PATH_RE.sub("[path]", cleaned)
    return cleaned[:240] or None


def _check_map(report: HealthReport) -> dict[str, HealthCheck]:
    return {check.label: check for check in report.checks}


def _health_result(raw: str) -> str:
    value = (raw or "").strip().upper()
    if value in {"PASS", "WARN", "FAIL"}:
        return value
    if value in {"READY"}:
        return "PASS"
    if value in {"NOT READY"}:
        return "FAIL"
    return "WARN"


def _service_health(raw: str) -> str:
    value = (raw or "").strip()
    upper = value.upper()
    if upper in {"PASS", "WARN", "FAIL"}:
        return upper
    if value in {"not yet available", "unknown", ""}:
        return "UNKNOWN"
    return "WARN"


def _service_state(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value in {"pass", "active", "ready"}:
        return "active"
    if value in {"inactive", "deactivating"}:
        return "inactive"
    if value in {"fail", "failed", "failure"}:
        return "failed"
    if value in {"activating"}:
        return "activating"
    return "unknown"


def _parse_usage_percent(detail: str | None) -> float | None:
    if not detail:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", detail)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _upload_summary(mk04_env_token: str, checks: dict[str, HealthCheck]) -> UploadStateSummary:
    check = checks.get("Upload safety state")
    status = "unknown"
    detail = None
    if check is not None:
        status = check.result.lower() if check.result else "unknown"
        if status in {"pass", "warn", "fail"}:
            status = status
        elif check.result in {"not yet available", "unknown"}:
            status = "unknown"
        detail = sanitize_detail(check.detail)

    config_enabled: bool | None = None
    runtime_disabled: bool | None = None
    enabled: bool | None = None
    try:
        canonical = canonical_env(mk04_env_token)
        token = mk04_env(canonical)
        resolved = ConfigManager.load(
            environment=canonical,
            config_root=REPO_ROOT / "config",
        )
        state = EnvironmentStatePaths.from_resolved_config(resolved)
        config_enabled = bool(resolved.uploading_enabled)
        runtime_disabled, _ = load_runtime_upload_control(state.data_root)
        enabled, effective_detail = compute_effective_upload(config_enabled, runtime_disabled)
        if detail is None:
            detail = sanitize_detail(effective_detail)
    except Exception:
        # Keep check-derived status; leave enabled fields unknown.
        pass

    return UploadStateSummary(
        enabled=enabled,
        config_enabled=config_enabled,
        runtime_disabled=runtime_disabled,
        status=status,
        detail=detail,
    )


def _scheduler_summary(
    mk04_env_token: str, checks: dict[str, HealthCheck]
) -> SchedulerStateSummary:
    check = checks.get("Scheduler")
    status = "unknown"
    detail = None
    if check is not None:
        status = (check.result or "unknown").lower()
        detail = sanitize_detail(check.detail)

    token = mk04_env(canonical_env(mk04_env_token))
    runtime_disabled: bool | None = None
    underlying_active: bool | None = None
    mechanism: str | None = None
    effective = "unknown"
    try:
        data_root = REPO_ROOT / "data" / token
        try:
            canonical = canonical_env(mk04_env_token)
            resolved = ConfigManager.load(
                environment=canonical,
                config_root=REPO_ROOT / "config",
            )
            data_root = EnvironmentStatePaths.from_resolved_config(resolved).data_root
        except ConfigError:
            pass
        runtime_disabled, _ = load_runtime_scheduler_control(data_root)
        underlying = inspect_underlying_scheduler(token, REPO_ROOT)
        underlying_active = underlying.active
        mechanism = underlying.mechanism
        effective, effective_detail = compute_effective_scheduler(
            runtime_disabled,
            underlying,
            mk04_env_token=token,
        )
        if detail is None:
            detail = sanitize_detail(effective_detail)
        mode = DEFAULT_SCHEDULER_MODE.get(token, "unknown")
        if mode == "manual" and status == "pass":
            effective = "manual"
    except Exception:
        pass

    return SchedulerStateSummary(
        effective=effective,
        runtime_disabled=runtime_disabled,
        underlying_active=underlying_active,
        mechanism=mechanism,
        status=status,
        detail=detail,
    )


def _disk_state(checks: dict[str, HealthCheck]) -> DiskState:
    check = checks.get("Disk pressure")
    if check is None:
        return DiskState(status="WARN", detail="disk pressure not reported")
    pressure_state = parse_storage_state_from_detail(check.detail)
    retention_recommended = False
    if pressure_state:
        try:
            level = DiskPressureLevel(pressure_state)
            retention_recommended = retention_recommended_for_level(level)
        except ValueError:
            retention_recommended = False
    return DiskState(
        status=_health_result(check.result),
        usage_percent=_parse_usage_percent(check.detail),
        detail=sanitize_detail(check.detail),
        pressure_state=pressure_state,
        retention_recommended=retention_recommended,
    )


def _execution_lock_summary(mk04_env_token: str) -> ExecutionLockSummary:
    try:
        inspection = inspect_execution_lock(mk04_env_token)
    except Exception as exc:
        return ExecutionLockSummary(
            present=False,
            stale=False,
            detail=sanitize_detail(f"lock inspection failed: {exc}"),
        )
    payload = inspection.payload
    return ExecutionLockSummary(
        present=bool(inspection.present),
        stale=bool(inspection.stale),
        run_id=payload.run_id if payload else None,
        trigger=payload.trigger if payload else None,
        started_at=payload.started_at if payload else None,
        pid=payload.pid if payload else None,
        detail=sanitize_detail(inspection.detail),
        os_lock_held=bool(inspection.os_lock_held),
        metadata_authoritative=bool(inspection.metadata_authoritative),
    )


def _execution_gate_summary() -> ExecutionGateSummary:
    try:
        from execution_gate import read_gate_status  # noqa: PLC0415

        snap = read_gate_status()
    except Exception as exc:
        return ExecutionGateSummary(
            state="free",
            detail=sanitize_detail(f"gate inspection failed: {exc}"),
            metadata_authoritative=False,
        )
    return ExecutionGateSummary(
        state=str(snap.state or "free"),
        owning_environment=snap.owning_environment,
        run_id=snap.run_id,
        pid=snap.pid,
        trigger=snap.trigger,
        requested_at=snap.requested_at,
        job_id=snap.job_id,
        stage=snap.stage,
        detail=sanitize_detail(snap.detail),
        metadata_authoritative=bool(snap.metadata_authoritative),
        shared_lock_root=snap.shared_lock_root,
    )


def _readiness_failures(report: HealthReport) -> list[str]:
    failures: list[str] = []
    if report.boot is not None:
        for component in report.boot.components:
            if component.required and component.result == "FAIL":
                failures.append(component.label)
    for check in report.checks:
        if check.result == "FAIL" or check.severity == "fail":
            if check.label not in failures:
                failures.append(check.label)
    return failures


def _service_status_from_check(
    *,
    service_name: str,
    result: str,
    detail: str | None,
    unit_name: str | None,
    checked_at: str,
) -> ServiceStatus:
    return ServiceStatus(
        service_name=service_name,
        state=_service_state(result),
        health=_service_health(result),
        last_checked_at=checked_at,
        restart_count=None,
        last_restart_at=None,
        detail=sanitize_detail(detail),
        unit_name=unit_name,
    )


def service_statuses_from_health_report(
    report: HealthReport, *, checked_at: str | None = None
) -> list[ServiceStatus]:
    """Map health-report service checks onto ServiceStatus models."""
    now = checked_at or _utc_now_iso()
    checks = _check_map(report)
    services: list[ServiceStatus] = []
    seen: set[str] = set()

    # Prefer discovered units so we only expose services that exist in deploy/.
    for label, unit in discover_service_units(REPO_ROOT):
        service_name = _LABEL_TO_SERVICE_NAME.get(label)
        if not service_name:
            continue
        check = checks.get(label)
        if label == "API" and check is None:
            check = checks.get("API health endpoint")
        if check is None:
            value, detail, _severity = systemd_unit_status(unit)
            services.append(
                _service_status_from_check(
                    service_name=service_name,
                    result=value,
                    detail=detail,
                    unit_name=unit,
                    checked_at=now,
                )
            )
        else:
            services.append(
                _service_status_from_check(
                    service_name=service_name,
                    result=check.result,
                    detail=check.detail,
                    unit_name=unit,
                    checked_at=now,
                )
            )
        seen.add(service_name)

    # Scheduler is operational but not a systemd unit today.
    scheduler_check = checks.get("Scheduler")
    if scheduler_check is not None and "scheduler" not in seen:
        services.append(
            _service_status_from_check(
                service_name="scheduler",
                result=scheduler_check.result,
                detail=scheduler_check.detail,
                unit_name=None,
                checked_at=now,
            )
        )

    return services


def build_system_health(
    mk04_env_token: str,
    *,
    report: HealthReport | None = None,
) -> SystemHealth:
    """Populate SystemHealth from existing health_report infrastructure."""
    token = mk04_env(canonical_env(mk04_env_token))
    checked_at = _utc_now_iso()
    try:
        health_report = report if report is not None else build_health_report(mk04_env_token)
    except Exception as exc:
        return SystemHealth(
            overall="FAIL",
            environment=token,
            readiness_failures=[f"health inspection failed: {exc.__class__.__name__}"],
            checked_at=checked_at,
            schema_version=CONTRACT_SCHEMA_VERSION,
        )

    checks = _check_map(health_report)
    boot_readiness = None
    if health_report.boot is not None:
        boot_readiness = health_report.boot.overall

    return SystemHealth(
        overall=_health_result(health_report.overall),
        environment=token,
        disk=_disk_state(checks),
        upload=_upload_summary(token, checks),
        scheduler=_scheduler_summary(token, checks),
        services=service_statuses_from_health_report(health_report, checked_at=checked_at),
        readiness_failures=_readiness_failures(health_report),
        execution_lock=_execution_lock_summary(token),
        execution_gate=_execution_gate_summary(),
        boot_readiness=boot_readiness,
        checked_at=checked_at,
        schema_version=CONTRACT_SCHEMA_VERSION,
    )


def build_service_statuses(mk04_env_token: str) -> list[ServiceStatus]:
    """Populate ServiceStatus list from existing health/service inspection."""
    try:
        report = build_health_report(mk04_env_token)
        return service_statuses_from_health_report(report)
    except Exception as exc:
        checked_at = _utc_now_iso()
        return [
            ServiceStatus(
                service_name="unknown",
                state="unknown",
                health="FAIL",
                last_checked_at=checked_at,
                detail=sanitize_detail(f"service inspection failed: {exc}"),
            )
        ]


def _activity_value(report: StatusReport, label: str) -> str | None:
    for line in report.activity_lines:
        if line.label == label:
            return line.value
    return None


def _parse_int_or_none(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in {"", "not yet available", "unknown", "none"}:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _queue_summary(report: StatusReport) -> QueueSummary:
    pending = _parse_int_or_none(_activity_value(report, "Queue pending"))
    running = _parse_int_or_none(_activity_value(report, "Running jobs"))
    failed = _parse_int_or_none(_activity_value(report, "Failed jobs today"))
    return QueueSummary(
        pending=pending if pending is not None else 0,
        running=running if running is not None else 0,
        failed=failed if failed is not None else 0,
    )


def _active_run(mk04_env_token: str) -> ActiveRunRef | None:
    try:
        inspection = inspect_execution_lock(mk04_env_token)
    except Exception:
        inspection = None

    if inspection is not None and inspection.present and inspection.payload is not None:
        payload = inspection.payload
        status = "RUNNING"
        if inspection.stale:
            status = "unknown"
        return ActiveRunRef(
            run_id=payload.run_id,
            trigger=payload.trigger or None,
            started_at=payload.started_at or None,
            funnel_id=payload.funnel_id or None,
            status=status,
        )

    try:
        record = latest_run_record(mk04_env_token)
    except Exception:
        record = None
    if record is not None and record.status == "RUNNING":
        return ActiveRunRef(
            run_id=record.run_id,
            trigger=record.trigger or None,
            started_at=record.started_at or None,
            funnel_id=record.funnel_id or None,
            status=record.status,
        )
    return None


def _activity_state(
    *,
    active_run: ActiveRunRef | None,
    lock: ExecutionLockSummary,
    queue: QueueSummary,
) -> str:
    if lock.present and lock.stale:
        return "blocked"
    if active_run is not None and active_run.status == "RUNNING":
        return "running"
    if lock.present and not lock.stale:
        return "running"
    if queue.running > 0:
        return "running"
    return "idle"


def _current_activity(
    *,
    active_run: ActiveRunRef | None,
    lock: ExecutionLockSummary,
    queue: QueueSummary,
    pending_known: bool,
) -> str | None:
    del pending_known  # pending counts are not yet exposed to operators.
    if lock.present and lock.stale:
        return "Blocked — stale execution lock needs attention."
    if active_run is not None:
        funnel = f" ({active_run.funnel_id})" if active_run.funnel_id else ""
        return f"Pipeline run in progress{funnel}."
    if queue.running > 0:
        job_word = "job" if queue.running == 1 else "jobs"
        return (
            f"{queue.running} video {job_word} processing. "
            "No pipeline run active."
        )
    if queue.failed > 0:
        fail_word = "failure" if queue.failed == 1 else "failures"
        return f"Nothing running. {queue.failed} job {fail_word} today."
    return "Nothing in progress."


def _recent_summary(report: StatusReport, mk04_env_token: str) -> RecentActivitySummary:
    jobs_failed = _parse_int_or_none(_activity_value(report, "Failed jobs today"))
    runs = 0
    jobs_completed = 0
    try:
        from run_records import list_run_dirs, read_record

        today = datetime.now(UTC).date().isoformat()
        for run_dir in list_run_dirs(mk04_env_token)[:50]:
            record = read_record(run_dir)
            if record is None:
                continue
            started = record.started_at or ""
            if not started.startswith(today):
                continue
            runs += 1
            if record.status == "SUCCESS":
                jobs_completed += int(record.jobs_completed or 0)
    except Exception:
        # Leave run counters at 0 when run-record scan is unavailable.
        runs = 0
        jobs_completed = 0

    return RecentActivitySummary(
        runs=runs,
        jobs_completed=jobs_completed,
        jobs_failed=jobs_failed if jobs_failed is not None else 0,
        # Clip/post counters are not yet available from status infrastructure.
        clips_created=0,
        posts_attempted=0,
        window="today",
    )


def build_system_status(
    mk04_env_token: str,
    *,
    report: StatusReport | None = None,
) -> SystemStatus:
    """Populate SystemStatus from existing status_report / lock infrastructure."""
    token = mk04_env(canonical_env(mk04_env_token))
    checked_at = _utc_now_iso()
    try:
        status_report = report if report is not None else build_status_report(mk04_env_token)
    except Exception as exc:
        return SystemStatus(
            environment=token,
            state="unknown",
            active_run=None,
            current_activity=sanitize_detail(f"status inspection failed: {exc}"),
            checked_at=checked_at,
            schema_version=CONTRACT_SCHEMA_VERSION,
        )

    lock = _execution_lock_summary(token)
    active_run = _active_run(token)
    queue = _queue_summary(status_report)
    pending_raw = _activity_value(status_report, "Queue pending")
    pending_known = _parse_int_or_none(pending_raw) is not None

    state = _activity_state(active_run=active_run, lock=lock, queue=queue)
    # StatusReport uses PASS/WARN/FAIL for overall readiness-ish signal; activity
    # state stays idle/running/blocked unless we have an active failure signal.
    if status_report.overall == "FAIL" and state == "idle":
        # Config/boot failure is readiness, not current activity — keep idle.
        pass

    return SystemStatus(
        environment=token,
        state=state,
        active_run=active_run,
        queue=queue,
        current_activity=_current_activity(
            active_run=active_run,
            lock=lock,
            queue=queue,
            pending_known=pending_known,
        ),
        recent_summary=_recent_summary(status_report, token),
        checked_at=checked_at,
        schema_version=CONTRACT_SCHEMA_VERSION,
    )


def services_payload(mk04_env_token: str) -> dict[str, Any]:
    """Envelope for GET /services."""
    token = mk04_env(canonical_env(mk04_env_token))
    services = build_service_statuses(mk04_env_token)
    return {
        "environment": token,
        "checked_at": _utc_now_iso(),
        "services": [service.to_dict() for service in services],
        "schema_version": CONTRACT_SCHEMA_VERSION,
    }
