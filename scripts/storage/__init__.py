"""Storage & Data Management.

Phase 1: artifact type names.
Phase 2: retention policy configuration (config layer only).
Phase 3: artifact classification (metadata only — no retention or deletion).
Phase 4: retention dry-run planner (policy evaluation — no deletion).
Phase 5: safe apply executor (executes approved plan with safety checks).
Phase 6: versioned retention reports (stable operational interface).
Phase 7: disk pressure checks (classification, health, production job gate).
Phase 8: scheduled retention (config-driven caller of planner/apply).
Phase 9: log rotation (bounds active logs; retention owns expiry).
Phase 10: database backup (SQLite snapshots; retention owns expiry).
"""

from .artifact_classifier import ArtifactClassifier, classify_artifact
from .artifact_record import ArtifactRecord, DeletionEligibility
from .artifact_types import (
    ARTIFACT_TYPES,
    BUSINESS_OUTPUT_TYPES,
    OPERATIONAL_EVIDENCE_TYPES,
    OWNER_BY_TYPE,
    PROTECTION_FLAGS,
    TEMPORARY_TYPES,
)
from .database_backup import (
    DatabaseBackupConfig,
    DatabaseBackupResult,
    load_database_backup_config,
    load_latest_backup_record,
    run_database_backup,
)
from .log_rotation import (
    LogRotationConfig,
    LogRotationResult,
    load_log_rotation_config,
    load_latest_rotation_record,
    render_journald_dropin,
    render_logrotate_config,
    run_log_rotation,
)
from .disk_pressure import (
    DiskPressureLevel,
    DiskPressureStatus,
    DiskPressureThresholds,
    JobStartGateResult,
    can_start_new_job,
    classify_disk_pressure,
    evaluate_disk_pressure,
    load_disk_pressure_thresholds,
    record_disk_pressure_block,
)
from .retention_apply import RetentionApplyExecutor, run_retention_apply
from .retention_planner import RetentionPlanner, run_retention_dry_run
from .retention_schedule import (
    RetentionScheduleConfig,
    ScheduledRetentionResult,
    load_latest_scheduled_retention,
    load_retention_schedule_config,
    run_scheduled_retention,
)
from .retention_report import (
    APPLY_VERSION,
    LATEST_POINTER_NAME,
    PLANNER_VERSION,
    RETENTION_REPORT_SCHEMA_VERSION,
    DeletionRecord,
    RetentionApplyReport,
    RetentionFileDecision,
    RetentionPlanReport,
    format_apply_terminal_summary,
    format_terminal_summary,
    load_latest_retention_report,
    load_plan_report,
    load_retention_report,
    write_retention_report,
)

__all__ = [
    "ARTIFACT_TYPES",
    "BUSINESS_OUTPUT_TYPES",
    "OPERATIONAL_EVIDENCE_TYPES",
    "OWNER_BY_TYPE",
    "PROTECTION_FLAGS",
    "TEMPORARY_TYPES",
    "APPLY_VERSION",
    "LATEST_POINTER_NAME",
    "PLANNER_VERSION",
    "RETENTION_REPORT_SCHEMA_VERSION",
    "ArtifactClassifier",
    "ArtifactRecord",
    "DeletionEligibility",
    "classify_artifact",
    "can_start_new_job",
    "classify_disk_pressure",
    "evaluate_disk_pressure",
    "load_disk_pressure_thresholds",
    "record_disk_pressure_block",
    "DeletionRecord",
    "DiskPressureLevel",
    "DiskPressureStatus",
    "DiskPressureThresholds",
    "JobStartGateResult",
    "DatabaseBackupConfig",
    "DatabaseBackupResult",
    "LogRotationConfig",
    "LogRotationResult",
    "load_database_backup_config",
    "load_latest_backup_record",
    "load_log_rotation_config",
    "load_latest_rotation_record",
    "render_journald_dropin",
    "render_logrotate_config",
    "run_database_backup",
    "run_log_rotation",
    "RetentionApplyExecutor",
    "RetentionApplyReport",
    "RetentionPlanner",
    "RetentionScheduleConfig",
    "ScheduledRetentionResult",
    "load_latest_scheduled_retention",
    "load_retention_schedule_config",
    "run_scheduled_retention",
    "RetentionFileDecision",
    "RetentionPlanReport",
    "format_apply_terminal_summary",
    "format_terminal_summary",
    "load_latest_retention_report",
    "load_plan_report",
    "load_retention_report",
    "run_retention_apply",
    "run_retention_dry_run",
    "write_retention_report",
]
