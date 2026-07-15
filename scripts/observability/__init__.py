"""Operations & Observability data contract.

Stable, UI-independent structured objects describing operational state.
SSH tooling, future JSON endpoints, and the Operations UI must all consume
these models rather than inventing parallel views of the system.

This package defines the contract only. It does not read the filesystem,
expose Flask routes, or render UI pages.
"""

from __future__ import annotations

from .contract import (
    CONTRACT_SCHEMA_VERSION,
    assert_config_summary_safe,
    config_summary_from_operational_state,
    is_secret_field_name,
    run_summary_from_run_record_dict,
)
from .envelope import (
    API_ENVELOPE_SCHEMA_VERSION,
    not_found_error,
    observability_envelope,
)
from .models import (
    ActiveRunRef,
    ArtifactReference,
    ClipSummary,
    ConfigSummary,
    DiskState,
    ExecutionGateSummary,
    ExecutionLockSummary,
    FailureGroup,
    FailureSummary,
    JobDetail,
    JobOutputs,
    JobSummary,
    LogEntry,
    LogReference,
    QueueSummary,
    RecentActivitySummary,
    RunSummary,
    SchedulerStateSummary,
    ServiceStatus,
    StageTimelineEntry,
    SystemHealth,
    SystemStatus,
    UploadStateSummary,
)
from .schemas import (
    ACTIVITY_STATES,
    ARTIFACT_TYPES,
    CLIP_POSTING_STATES,
    CLIP_VALIDATION_STATES,
    FAILURE_SEVERITIES,
    HEALTH_RESULTS,
    JOB_STATES,
    LOG_SOURCES,
    RUN_STATUSES,
    RUN_TRIGGERS,
    SERVICE_HEALTH_RESULTS,
    SERVICE_STATES,
    STAGE_NAMES,
    STAGE_RESULTS,
)

__all__ = [
    "API_ENVELOPE_SCHEMA_VERSION",
    "CONTRACT_SCHEMA_VERSION",
    "ACTIVITY_STATES",
    "ARTIFACT_TYPES",
    "CLIP_POSTING_STATES",
    "CLIP_VALIDATION_STATES",
    "FAILURE_SEVERITIES",
    "HEALTH_RESULTS",
    "JOB_STATES",
    "LOG_SOURCES",
    "RUN_STATUSES",
    "RUN_TRIGGERS",
    "SERVICE_HEALTH_RESULTS",
    "SERVICE_STATES",
    "STAGE_NAMES",
    "STAGE_RESULTS",
    "ActiveRunRef",
    "ArtifactReference",
    "ClipSummary",
    "ConfigSummary",
    "DiskState",
    "ExecutionGateSummary",
    "ExecutionLockSummary",
    "FailureGroup",
    "FailureSummary",
    "JobDetail",
    "JobOutputs",
    "JobSummary",
    "LogEntry",
    "LogReference",
    "QueueSummary",
    "RecentActivitySummary",
    "RunSummary",
    "SchedulerStateSummary",
    "ServiceStatus",
    "StageTimelineEntry",
    "SystemHealth",
    "SystemStatus",
    "UploadStateSummary",
    "assert_config_summary_safe",
    "config_summary_from_operational_state",
    "is_secret_field_name",
    "not_found_error",
    "observability_envelope",
    "run_summary_from_run_record_dict",
]


def __getattr__(name: str):
    """Lazy-load populate helpers (depend on scripts/ops + PyYAML)."""
    if name in {
        "build_service_statuses",
        "build_system_health",
        "build_system_status",
        "services_payload",
    }:
        from . import populate

        return getattr(populate, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
