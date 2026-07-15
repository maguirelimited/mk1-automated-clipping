"""Structured observability models.

Pure data objects. No filesystem access, no Flask, no UI rendering.
Missing optional data is represented with None / empty collections / exists=False
rather than exceptions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .schemas import CONTRACT_SCHEMA_VERSION


def _as_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, list):
        return [_as_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): _as_dict(v) for k, v in obj.items()}
    return obj


@dataclass
class FailureSummary:
    """One operational failure or warning."""

    component: str
    reason: str
    severity: str = "fail"
    stage: str | None = None
    timestamp: str | None = None
    suggested_next_inspection_target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> FailureSummary | None:
        if not data or not isinstance(data, dict):
            return None
        reason = str(data.get("reason") or "").strip()
        component = str(data.get("component") or "").strip()
        if not reason and not component:
            return None
        return cls(
            component=component or "unknown",
            reason=reason or "unspecified",
            severity=str(data.get("severity") or "fail"),
            stage=_optional_str(data.get("stage")),
            timestamp=_optional_str(data.get("timestamp")),
            suggested_next_inspection_target=_optional_str(
                data.get("suggested_next_inspection_target")
            ),
        )


@dataclass
class FailureGroup:
    """Aggregated failure group for the Failures page (no new persistence)."""

    group_key: str
    category: str
    name: str
    count: int = 0
    severity: str = "ERROR"
    first_occurrence: str | None = None
    latest_occurrence: str | None = None
    affected_jobs: list[str] = field(default_factory=list)
    affected_runs: list[str] = field(default_factory=list)
    affected_stage: str | None = None
    affected_module: str | None = None
    representative_reason: str = "Not available"
    suggested_next_inspection_target: str = "Not available"

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_key": self.group_key,
            "category": self.category,
            "name": self.name,
            "count": self.count,
            "severity": self.severity,
            "first_occurrence": self.first_occurrence,
            "latest_occurrence": self.latest_occurrence,
            "affected_jobs": list(self.affected_jobs),
            "affected_runs": list(self.affected_runs),
            "affected_stage": self.affected_stage,
            "affected_module": self.affected_module,
            "representative_reason": self.representative_reason,
            "suggested_next_inspection_target": self.suggested_next_inspection_target,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> FailureGroup | None:
        if not data or not isinstance(data, dict):
            return None
        group_key = str(data.get("group_key") or "").strip()
        if not group_key:
            return None
        jobs = data.get("affected_jobs") or []
        runs = data.get("affected_runs") or []
        if not isinstance(jobs, list):
            jobs = []
        if not isinstance(runs, list):
            runs = []
        return cls(
            group_key=group_key,
            category=str(data.get("category") or "unknown"),
            name=str(data.get("name") or "unknown"),
            count=int(data.get("count") or 0),
            severity=str(data.get("severity") or "ERROR"),
            first_occurrence=_optional_str(data.get("first_occurrence")),
            latest_occurrence=_optional_str(data.get("latest_occurrence")),
            affected_jobs=[str(j) for j in jobs],
            affected_runs=[str(r) for r in runs],
            affected_stage=_optional_str(data.get("affected_stage")),
            affected_module=_optional_str(data.get("affected_module")),
            representative_reason=str(
                data.get("representative_reason") or "Not available"
            ),
            suggested_next_inspection_target=str(
                data.get("suggested_next_inspection_target") or "Not available"
            ),
        )


@dataclass
class ServiceStatus:
    """One long-running service."""

    service_name: str
    state: str = "unknown"
    health: str = "UNKNOWN"
    last_checked_at: str | None = None
    restart_count: int | None = None
    last_restart_at: str | None = None
    detail: str | None = None
    unit_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ServiceStatus | None:
        if not data or not isinstance(data, dict):
            return None
        name = str(data.get("service_name") or "").strip()
        if not name:
            return None
        restart_count = data.get("restart_count")
        try:
            restart_count_int = int(restart_count) if restart_count is not None else None
        except (TypeError, ValueError):
            restart_count_int = None
        return cls(
            service_name=name,
            state=str(data.get("state") or "unknown"),
            health=str(data.get("health") or "UNKNOWN"),
            last_checked_at=_optional_str(data.get("last_checked_at")),
            restart_count=restart_count_int,
            last_restart_at=_optional_str(data.get("last_restart_at")),
            detail=_optional_str(data.get("detail")),
            unit_name=_optional_str(data.get("unit_name")),
        )


@dataclass
class DiskState:
    """Disk readiness for SystemHealth."""

    status: str = "WARN"
    usage_percent: float | None = None
    detail: str | None = None
    pressure_state: str | None = None
    free_bytes: int | None = None
    total_bytes: int | None = None
    retention_recommended: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DiskState:
        if not data or not isinstance(data, dict):
            return cls()
        usage = data.get("usage_percent")
        try:
            usage_f = float(usage) if usage is not None else None
        except (TypeError, ValueError):
            usage_f = None
        free_bytes = data.get("free_bytes")
        total_bytes = data.get("total_bytes")
        try:
            free_i = int(free_bytes) if free_bytes is not None else None
        except (TypeError, ValueError):
            free_i = None
        try:
            total_i = int(total_bytes) if total_bytes is not None else None
        except (TypeError, ValueError):
            total_i = None
        retention = data.get("retention_recommended")
        return cls(
            status=str(data.get("status") or "WARN"),
            usage_percent=usage_f,
            detail=_optional_str(data.get("detail")),
            pressure_state=_optional_str(data.get("pressure_state")),
            free_bytes=free_i,
            total_bytes=total_i,
            retention_recommended=bool(retention) if retention is not None else False,
        )


@dataclass
class UploadStateSummary:
    """Effective upload / posting state (no secrets)."""

    enabled: bool | None = None
    config_enabled: bool | None = None
    runtime_disabled: bool | None = None
    status: str = "unknown"
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> UploadStateSummary:
        if not data or not isinstance(data, dict):
            return cls()
        return cls(
            enabled=_optional_bool(data.get("enabled")),
            config_enabled=_optional_bool(data.get("config_enabled")),
            runtime_disabled=_optional_bool(data.get("runtime_disabled")),
            status=str(data.get("status") or "unknown"),
            detail=_optional_str(data.get("detail")),
        )


@dataclass
class SchedulerStateSummary:
    """Effective scheduler state (no secrets)."""

    effective: str = "unknown"
    runtime_disabled: bool | None = None
    underlying_active: bool | None = None
    mechanism: str | None = None
    status: str = "unknown"
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SchedulerStateSummary:
        if not data or not isinstance(data, dict):
            return cls()
        return cls(
            effective=str(data.get("effective") or "unknown"),
            runtime_disabled=_optional_bool(data.get("runtime_disabled")),
            underlying_active=_optional_bool(data.get("underlying_active")),
            mechanism=_optional_str(data.get("mechanism")),
            status=str(data.get("status") or "unknown"),
            detail=_optional_str(data.get("detail")),
        )


@dataclass
class ExecutionLockSummary:
    """Read-only view of the per-environment pipeline execution lock."""

    present: bool = False
    stale: bool = False
    run_id: str | None = None
    trigger: str | None = None
    started_at: str | None = None
    pid: int | None = None
    detail: str | None = None
    os_lock_held: bool = False
    metadata_authoritative: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ExecutionLockSummary:
        if not data or not isinstance(data, dict):
            return cls()
        pid_raw = data.get("pid")
        try:
            pid = int(pid_raw) if pid_raw is not None else None
        except (TypeError, ValueError):
            pid = None
        return cls(
            present=bool(data.get("present")),
            stale=bool(data.get("stale")),
            run_id=_optional_str(data.get("run_id")),
            trigger=_optional_str(data.get("trigger")),
            started_at=_optional_str(data.get("started_at")),
            pid=pid,
            detail=_optional_str(data.get("detail")),
            os_lock_held=bool(data.get("os_lock_held")),
            metadata_authoritative=bool(data.get("metadata_authoritative")),
        )


@dataclass
class ExecutionGateSummary:
    """Cross-environment execution gate (shared lock root)."""

    state: str = "free"
    owning_environment: str | None = None
    run_id: str | None = None
    pid: int | None = None
    trigger: str | None = None
    requested_at: str | None = None
    job_id: str | None = None
    stage: str | None = None
    detail: str | None = None
    metadata_authoritative: bool = False
    shared_lock_root: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ExecutionGateSummary:
        if not data or not isinstance(data, dict):
            return cls()
        pid_raw = data.get("pid")
        try:
            pid = int(pid_raw) if pid_raw is not None else None
        except (TypeError, ValueError):
            pid = None
        return cls(
            state=str(data.get("state") or "free"),
            owning_environment=_optional_str(data.get("owning_environment")),
            run_id=_optional_str(data.get("run_id")),
            pid=pid,
            trigger=_optional_str(data.get("trigger")),
            requested_at=_optional_str(data.get("requested_at")),
            job_id=_optional_str(data.get("job_id")),
            stage=_optional_str(data.get("stage")),
            detail=_optional_str(data.get("detail")),
            metadata_authoritative=bool(data.get("metadata_authoritative")),
            shared_lock_root=_optional_str(data.get("shared_lock_root")),
        )


@dataclass
class SystemHealth:
    """Readiness: is the system safe and ready?"""

    overall: str
    environment: str
    disk: DiskState = field(default_factory=DiskState)
    upload: UploadStateSummary = field(default_factory=UploadStateSummary)
    scheduler: SchedulerStateSummary = field(default_factory=SchedulerStateSummary)
    services: list[ServiceStatus] = field(default_factory=list)
    readiness_failures: list[str] = field(default_factory=list)
    execution_lock: ExecutionLockSummary | None = None
    execution_gate: ExecutionGateSummary | None = None
    boot_readiness: str | None = None
    checked_at: str | None = None
    schema_version: int = CONTRACT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall,
            "environment": self.environment,
            "disk": self.disk.to_dict(),
            "upload": self.upload.to_dict(),
            "scheduler": self.scheduler.to_dict(),
            "services": [s.to_dict() for s in self.services],
            "readiness_failures": list(self.readiness_failures),
            "execution_lock": _as_dict(self.execution_lock),
            "execution_gate": _as_dict(self.execution_gate),
            "boot_readiness": self.boot_readiness,
            "checked_at": self.checked_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SystemHealth:
        services_raw = data.get("services") or []
        services: list[ServiceStatus] = []
        if isinstance(services_raw, list):
            for item in services_raw:
                status = ServiceStatus.from_dict(item if isinstance(item, dict) else None)
                if status is not None:
                    services.append(status)
        failures = data.get("readiness_failures") or []
        if not isinstance(failures, list):
            failures = []
        lock_raw = data.get("execution_lock")
        gate_raw = data.get("execution_gate")
        return cls(
            overall=str(data.get("overall") or "WARN"),
            environment=str(data.get("environment") or ""),
            disk=DiskState.from_dict(data.get("disk") if isinstance(data.get("disk"), dict) else None),
            upload=UploadStateSummary.from_dict(
                data.get("upload") if isinstance(data.get("upload"), dict) else None
            ),
            scheduler=SchedulerStateSummary.from_dict(
                data.get("scheduler") if isinstance(data.get("scheduler"), dict) else None
            ),
            services=services,
            readiness_failures=[str(f) for f in failures],
            execution_lock=(
                ExecutionLockSummary.from_dict(lock_raw)
                if isinstance(lock_raw, dict)
                else None
            ),
            execution_gate=(
                ExecutionGateSummary.from_dict(gate_raw)
                if isinstance(gate_raw, dict)
                else None
            ),
            boot_readiness=_optional_str(data.get("boot_readiness")),
            checked_at=_optional_str(data.get("checked_at")),
            schema_version=int(data.get("schema_version") or CONTRACT_SCHEMA_VERSION),
        )


@dataclass
class QueueSummary:
    """Queue counts for SystemStatus."""

    pending: int = 0
    running: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> QueueSummary:
        if not data or not isinstance(data, dict):
            return cls()
        return cls(
            pending=_int_or_zero(data.get("pending")),
            running=_int_or_zero(data.get("running")),
            failed=_int_or_zero(data.get("failed")),
        )


@dataclass
class RecentActivitySummary:
    """Recent activity counters for SystemStatus."""

    runs: int = 0
    jobs_completed: int = 0
    jobs_failed: int = 0
    clips_created: int = 0
    posts_attempted: int = 0
    window: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RecentActivitySummary:
        if not data or not isinstance(data, dict):
            return cls()
        return cls(
            runs=_int_or_zero(data.get("runs")),
            jobs_completed=_int_or_zero(data.get("jobs_completed")),
            jobs_failed=_int_or_zero(data.get("jobs_failed")),
            clips_created=_int_or_zero(data.get("clips_created")),
            posts_attempted=_int_or_zero(data.get("posts_attempted")),
            window=_optional_str(data.get("window")),
        )


@dataclass
class ActiveRunRef:
    """Lightweight pointer to the currently active pipeline run."""

    run_id: str
    trigger: str | None = None
    started_at: str | None = None
    funnel_id: str | None = None
    status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ActiveRunRef | None:
        if not data or not isinstance(data, dict):
            return None
        run_id = str(data.get("run_id") or "").strip()
        if not run_id:
            return None
        return cls(
            run_id=run_id,
            trigger=_optional_str(data.get("trigger")),
            started_at=_optional_str(data.get("started_at")),
            funnel_id=_optional_str(data.get("funnel_id")),
            status=_optional_str(data.get("status")),
        )


@dataclass
class SystemStatus:
    """Current activity: what is happening right now?"""

    environment: str
    state: str = "idle"
    active_run: ActiveRunRef | None = None
    queue: QueueSummary = field(default_factory=QueueSummary)
    current_activity: str | None = None
    recent_summary: RecentActivitySummary = field(default_factory=RecentActivitySummary)
    checked_at: str | None = None
    schema_version: int = CONTRACT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "state": self.state,
            "active_run": _as_dict(self.active_run),
            "queue": self.queue.to_dict(),
            "current_activity": self.current_activity,
            "recent_summary": self.recent_summary.to_dict(),
            "checked_at": self.checked_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SystemStatus:
        active_raw = data.get("active_run")
        return cls(
            environment=str(data.get("environment") or ""),
            state=str(data.get("state") or "idle"),
            active_run=(
                ActiveRunRef.from_dict(active_raw)
                if isinstance(active_raw, dict)
                else None
            ),
            queue=QueueSummary.from_dict(
                data.get("queue") if isinstance(data.get("queue"), dict) else None
            ),
            current_activity=_optional_str(data.get("current_activity")),
            recent_summary=RecentActivitySummary.from_dict(
                data.get("recent_summary")
                if isinstance(data.get("recent_summary"), dict)
                else None
            ),
            checked_at=_optional_str(data.get("checked_at")),
            schema_version=int(data.get("schema_version") or CONTRACT_SCHEMA_VERSION),
        )


@dataclass
class RunSummary:
    """Pipeline run list/detail summary. Wraps run_record.json fields."""

    run_id: str
    environment: str
    trigger: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    jobs_started: int = 0
    jobs_completed: int = 0
    jobs_failed: int = 0
    funnel_id: str | None = None
    failure_summary: FailureSummary | None = None
    log_path: str | None = None
    report_paths: list[str] = field(default_factory=list)
    schema_version: int = CONTRACT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "environment": self.environment,
            "trigger": self.trigger,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "jobs_started": self.jobs_started,
            "jobs_completed": self.jobs_completed,
            "jobs_failed": self.jobs_failed,
            "funnel_id": self.funnel_id,
            "failure_summary": _as_dict(self.failure_summary),
            "log_path": self.log_path,
            "report_paths": list(self.report_paths),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunSummary:
        failure_raw = data.get("failure_summary")
        reports = data.get("report_paths") or []
        if not isinstance(reports, list):
            reports = []
        duration = data.get("duration_seconds")
        try:
            duration_f = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_f = None
        return cls(
            run_id=str(data.get("run_id") or ""),
            environment=str(data.get("environment") or ""),
            trigger=str(data.get("trigger") or ""),
            status=str(data.get("status") or "UNKNOWN"),
            started_at=_optional_str(data.get("started_at")),
            finished_at=_optional_str(data.get("finished_at")),
            duration_seconds=duration_f,
            jobs_started=_int_or_zero(data.get("jobs_started")),
            jobs_completed=_int_or_zero(data.get("jobs_completed")),
            jobs_failed=_int_or_zero(data.get("jobs_failed")),
            funnel_id=_optional_str(data.get("funnel_id")),
            failure_summary=(
                FailureSummary.from_dict(failure_raw)
                if isinstance(failure_raw, dict)
                else None
            ),
            log_path=_optional_str(data.get("log_path")),
            report_paths=[str(p) for p in reports],
            schema_version=int(data.get("schema_version") or CONTRACT_SCHEMA_VERSION),
        )


@dataclass
class JobOutputs:
    """Output counts for a job list entry."""

    candidates_discovered: int | None = None
    clips_passed: int | None = None
    clips_failed: int | None = None
    outputs_produced: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> JobOutputs:
        if not data or not isinstance(data, dict):
            return cls()
        return cls(
            candidates_discovered=_optional_int(data.get("candidates_discovered")),
            clips_passed=_optional_int(data.get("clips_passed")),
            clips_failed=_optional_int(data.get("clips_failed")),
            outputs_produced=_optional_int(data.get("outputs_produced")),
        )


@dataclass
class JobSummary:
    """Job list entry."""

    job_id: str
    state: str = "unknown"
    environment: str | None = None
    run_id: str | None = None
    funnel: str | None = None
    platform: str | None = None
    preset: str | None = None
    stage: str | None = None
    runtime_seconds: float | None = None
    outputs: JobOutputs = field(default_factory=JobOutputs)
    failure_summary: FailureSummary | None = None
    schema_version: int = CONTRACT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state,
            "environment": self.environment,
            "run_id": self.run_id,
            "funnel": self.funnel,
            "platform": self.platform,
            "preset": self.preset,
            "stage": self.stage,
            "runtime_seconds": self.runtime_seconds,
            "outputs": self.outputs.to_dict(),
            "failure_summary": _as_dict(self.failure_summary),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobSummary:
        failure_raw = data.get("failure_summary")
        runtime = data.get("runtime_seconds")
        try:
            runtime_f = float(runtime) if runtime is not None else None
        except (TypeError, ValueError):
            runtime_f = None
        return cls(
            job_id=str(data.get("job_id") or ""),
            state=str(data.get("state") or "unknown"),
            environment=_optional_str(data.get("environment")),
            run_id=_optional_str(data.get("run_id")),
            funnel=_optional_str(data.get("funnel")),
            platform=_optional_str(data.get("platform")),
            preset=_optional_str(data.get("preset")),
            stage=_optional_str(data.get("stage")),
            runtime_seconds=runtime_f,
            outputs=JobOutputs.from_dict(
                data.get("outputs") if isinstance(data.get("outputs"), dict) else None
            ),
            failure_summary=(
                FailureSummary.from_dict(failure_raw)
                if isinstance(failure_raw, dict)
                else None
            ),
            schema_version=int(data.get("schema_version") or CONTRACT_SCHEMA_VERSION),
        )


@dataclass
class StageTimelineEntry:
    """One stage in a job's pipeline timeline."""

    stage: str
    result: str = "unknown"
    detail: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> StageTimelineEntry | None:
        if not data or not isinstance(data, dict):
            return None
        stage = str(data.get("stage") or "").strip()
        if not stage:
            return None
        return cls(
            stage=stage,
            result=str(data.get("result") or "unknown"),
            detail=_optional_str(data.get("detail")),
            started_at=_optional_str(data.get("started_at")),
            finished_at=_optional_str(data.get("finished_at")),
        )


@dataclass
class ArtifactReference:
    """One artifact. Missing artifacts are representable (exists=False)."""

    artifact_type: str
    path: str | None = None
    exists: bool = False
    environment: str | None = None
    job_id: str | None = None
    run_id: str | None = None
    created_at: str | None = None
    size_bytes: int | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ArtifactReference | None:
        if not data or not isinstance(data, dict):
            return None
        artifact_type = str(data.get("artifact_type") or "").strip()
        if not artifact_type:
            return None
        size_raw = data.get("size_bytes")
        try:
            size_bytes = int(size_raw) if size_raw is not None else None
        except (TypeError, ValueError):
            size_bytes = None
        return cls(
            artifact_type=artifact_type,
            path=_optional_str(data.get("path")),
            exists=bool(data.get("exists")),
            environment=_optional_str(data.get("environment")),
            job_id=_optional_str(data.get("job_id")),
            run_id=_optional_str(data.get("run_id")),
            created_at=_optional_str(data.get("created_at")),
            size_bytes=size_bytes,
            detail=_optional_str(data.get("detail")),
        )

    @classmethod
    def missing(
        cls,
        artifact_type: str,
        *,
        path: str | None = None,
        environment: str | None = None,
        job_id: str | None = None,
        run_id: str | None = None,
        detail: str | None = "not found",
    ) -> ArtifactReference:
        """Build a safe missing-artifact reference without raising."""
        return cls(
            artifact_type=artifact_type,
            path=path,
            exists=False,
            environment=environment,
            job_id=job_id,
            run_id=run_id,
            detail=detail,
        )


@dataclass
class ClipSummary:
    """One output clip (Output Browser / Job Inspector)."""

    clip_id: str
    job_id: str
    source_candidate: str | None = None
    validation_state: str = "unknown"
    posting_state: str = "unknown"
    metadata_reference: ArtifactReference | None = None
    output_path: str | None = None
    platform: str | None = None
    funnel: str | None = None
    environment: str | None = None
    preset: str | None = None
    preview_available: bool = False
    exists: bool = True
    created_at: str | None = None
    duration_seconds: float | None = None
    size_bytes: int | None = None
    warnings: list[str] = field(default_factory=list)
    reframe_summary: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "clip_id": self.clip_id,
            "job_id": self.job_id,
            "source_candidate": self.source_candidate,
            "validation_state": self.validation_state,
            "posting_state": self.posting_state,
            "metadata_reference": _as_dict(self.metadata_reference),
            "output_path": self.output_path,
            "platform": self.platform,
            "funnel": self.funnel,
            "environment": self.environment,
            "preset": self.preset,
            "preview_available": self.preview_available,
            "exists": self.exists,
            "created_at": self.created_at,
            "duration_seconds": self.duration_seconds,
            "size_bytes": self.size_bytes,
            "warnings": list(self.warnings),
        }
        if self.reframe_summary is not None:
            payload["reframe_summary"] = dict(self.reframe_summary)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClipSummary:
        meta_raw = data.get("metadata_reference")
        warnings = data.get("warnings") or []
        if not isinstance(warnings, list):
            warnings = []
        duration = data.get("duration_seconds")
        try:
            duration_f = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_f = None
        size_raw = data.get("size_bytes")
        try:
            size_bytes = int(size_raw) if size_raw is not None else None
        except (TypeError, ValueError):
            size_bytes = None
        reframe_raw = data.get("reframe_summary")
        reframe_summary = reframe_raw if isinstance(reframe_raw, dict) else None
        return cls(
            clip_id=str(data.get("clip_id") or ""),
            job_id=str(data.get("job_id") or ""),
            source_candidate=_optional_str(data.get("source_candidate")),
            validation_state=str(data.get("validation_state") or "unknown"),
            posting_state=str(data.get("posting_state") or "unknown"),
            metadata_reference=(
                ArtifactReference.from_dict(meta_raw)
                if isinstance(meta_raw, dict)
                else None
            ),
            output_path=_optional_str(data.get("output_path")),
            platform=_optional_str(data.get("platform")),
            funnel=_optional_str(data.get("funnel")),
            environment=_optional_str(data.get("environment")),
            preset=_optional_str(data.get("preset")),
            reframe_summary=reframe_summary,
            preview_available=bool(data.get("preview_available")),
            exists=bool(data.get("exists", True)),
            created_at=_optional_str(data.get("created_at")),
            duration_seconds=duration_f,
            size_bytes=size_bytes,
            warnings=[str(w) for w in warnings],
        )


@dataclass
class LogReference:
    """Log pointer without embedding large content."""

    source: str
    path: str | None = None
    job_id: str | None = None
    run_id: str | None = None
    timestamp_start: str | None = None
    timestamp_end: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> LogReference | None:
        if not data or not isinstance(data, dict):
            return None
        source = str(data.get("source") or "").strip()
        if not source:
            return None
        return cls(
            source=source,
            path=_optional_str(data.get("path")),
            job_id=_optional_str(data.get("job_id")),
            run_id=_optional_str(data.get("run_id")),
            timestamp_start=_optional_str(data.get("timestamp_start")),
            timestamp_end=_optional_str(data.get("timestamp_end")),
            detail=_optional_str(data.get("detail")),
        )


@dataclass
class LogEntry:
    """One bounded, redacted log line for operator inspection."""

    message: str
    source: str
    timestamp: str | None = None
    severity: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> LogEntry | None:
        if not data or not isinstance(data, dict):
            return None
        message = str(data.get("message") or "")
        source = str(data.get("source") or "").strip()
        if not source and not message:
            return None
        return cls(
            message=message,
            source=source or "unknown",
            timestamp=_optional_str(data.get("timestamp")),
            severity=_optional_str(data.get("severity")),
        )


@dataclass
class ConfigSummary:
    """Read-only operational configuration summary. No secrets."""

    environment: str
    active_preset: str | None = None
    funnel: str | None = None
    platform: str | None = None
    upload: UploadStateSummary = field(default_factory=UploadStateSummary)
    scheduler: SchedulerStateSummary = field(default_factory=SchedulerStateSummary)
    schema_version: int = CONTRACT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "active_preset": self.active_preset,
            "funnel": self.funnel,
            "platform": self.platform,
            "upload": self.upload.to_dict(),
            "scheduler": self.scheduler.to_dict(),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConfigSummary:
        return cls(
            environment=str(data.get("environment") or ""),
            active_preset=_optional_str(data.get("active_preset")),
            funnel=_optional_str(data.get("funnel")),
            platform=_optional_str(data.get("platform")),
            upload=UploadStateSummary.from_dict(
                data.get("upload") if isinstance(data.get("upload"), dict) else None
            ),
            scheduler=SchedulerStateSummary.from_dict(
                data.get("scheduler") if isinstance(data.get("scheduler"), dict) else None
            ),
            schema_version=int(data.get("schema_version") or CONTRACT_SCHEMA_VERSION),
        )


@dataclass
class JobDetail:
    """Complete inspected job for the Job Inspector.

    Built by the observability layer. UI templates render this object only.
    """

    job_id: str
    summary: JobSummary
    stage_timeline: list[StageTimelineEntry] = field(default_factory=list)
    artifacts: list[ArtifactReference] = field(default_factory=list)
    reports: list[ArtifactReference] = field(default_factory=list)
    logs: list[LogReference] = field(default_factory=list)
    warnings: list[FailureSummary] = field(default_factory=list)
    failures: list[FailureSummary] = field(default_factory=list)
    clips: list[ClipSummary] = field(default_factory=list)
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    trigger: str | None = None
    report_summaries: list[dict[str, Any]] = field(default_factory=list)
    output_summary: dict[str, Any] | None = None
    schema_version: int = CONTRACT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "summary": self.summary.to_dict(),
            "stage_timeline": [s.to_dict() for s in self.stage_timeline],
            "artifacts": [a.to_dict() for a in self.artifacts],
            "reports": [r.to_dict() for r in self.reports],
            "logs": [log.to_dict() for log in self.logs],
            "warnings": [w.to_dict() for w in self.warnings],
            "failures": [f.to_dict() for f in self.failures],
            "clips": [c.to_dict() for c in self.clips],
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "trigger": self.trigger,
            "report_summaries": list(self.report_summaries),
            "output_summary": self.output_summary,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobDetail:
        summary_raw = data.get("summary")
        if isinstance(summary_raw, dict):
            summary = JobSummary.from_dict(summary_raw)
        else:
            summary = JobSummary(job_id=str(data.get("job_id") or ""))

        def _list_map(key: str, mapper):
            raw = data.get(key) or []
            if not isinstance(raw, list):
                return []
            out = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                mapped = mapper(item)
                if mapped is not None:
                    out.append(mapped)
            return out

        report_summaries = data.get("report_summaries") or []
        if not isinstance(report_summaries, list):
            report_summaries = []
        output_summary = data.get("output_summary")
        if output_summary is not None and not isinstance(output_summary, dict):
            output_summary = None

        return cls(
            job_id=str(data.get("job_id") or summary.job_id),
            summary=summary,
            stage_timeline=_list_map("stage_timeline", StageTimelineEntry.from_dict),
            artifacts=_list_map("artifacts", ArtifactReference.from_dict),
            reports=_list_map("reports", ArtifactReference.from_dict),
            logs=_list_map("logs", LogReference.from_dict),
            warnings=_list_map("warnings", FailureSummary.from_dict),
            failures=_list_map("failures", FailureSummary.from_dict),
            clips=_list_map("clips", ClipSummary.from_dict),
            created_at=_optional_str(data.get("created_at")),
            started_at=_optional_str(data.get("started_at")),
            finished_at=_optional_str(data.get("finished_at")),
            trigger=_optional_str(data.get("trigger")),
            report_summaries=[s for s in report_summaries if isinstance(s, dict)],
            output_summary=output_summary,
            schema_version=int(data.get("schema_version") or CONTRACT_SCHEMA_VERSION),
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
