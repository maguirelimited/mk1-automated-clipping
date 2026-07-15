"""Allowed values and field catalogues for the observability contract.

These constants document the stable vocabulary shared by SSH tooling,
JSON endpoints, and the Operations UI. They do not enforce runtime
validation beyond what adapters choose to apply.
"""

from __future__ import annotations

# Contract schema version for serialized observability payloads.
# Increment when a breaking field rename/removal occurs.
CONTRACT_SCHEMA_VERSION = 1

# SystemHealth.overall and component health results.
HEALTH_RESULTS = frozenset({"PASS", "WARN", "FAIL"})

# ServiceStatus.state — logical service lifecycle, not systemd-specific.
SERVICE_STATES = frozenset(
    {
        "active",
        "inactive",
        "failed",
        "activating",
        "deactivating",
        "unknown",
    }
)

# ServiceStatus.health — readiness of one long-running service.
SERVICE_HEALTH_RESULTS = frozenset({"PASS", "WARN", "FAIL", "UNKNOWN"})

# SystemStatus.state — current activity, not readiness.
ACTIVITY_STATES = frozenset({"idle", "running", "failing", "blocked"})

# RunSummary.status — aligned with scripts/ops/run_records.py.
RUN_STATUSES = frozenset({"RUNNING", "SUCCESS", "FAIL", "SKIPPED"})

# RunSummary.trigger — aligned with Reliability & Recovery trigger sources.
RUN_TRIGGERS = frozenset(
    {"scheduled", "manual_cli", "operations_ui", "remote_ssh", "test"}
)

# JobSummary.state — operator-facing job lifecycle.
JOB_STATES = frozenset(
    {
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
        "needs_attention",
        "unknown",
    }
)

# Pipeline stage names for JobDetail.stage_timeline.
STAGE_NAMES = (
    "source",
    "transcript",
    "processing",
    "selection",
    "rendering",
    "formatting",
    "captions",
    "validation",
    "posting",
    "output_registration",
)

STAGE_RESULTS = frozenset(
    {"pending", "running", "completed", "failed", "skipped", "unknown"}
)

# ArtifactReference.artifact_type — known operational artifacts.
ARTIFACT_TYPES = frozenset(
    {
        "transcript",
        "raw_candidate_pool",
        "processing_report",
        "selection_result",
        "post_processing_report",
        "clip_metadata",
        "output_clip",
        "job_log",
        "run_log",
        "run_record",
        "resolved_config",
        "review",
        "task",
        "execution_context",
        "other",
    }
)

# ClipSummary states.
CLIP_VALIDATION_STATES = frozenset(
    {"pending", "passed", "failed", "skipped", "unknown"}
)
CLIP_POSTING_STATES = frozenset(
    {
        "not_applicable",
        "pending",
        "queued",
        "uploading",
        "posted",
        "failed",
        "disabled",
        "unknown",
    }
)

# FailureSummary.severity.
FAILURE_SEVERITIES = frozenset({"info", "warn", "fail", "critical"})

# LogReference.source.
LOG_SOURCES = frozenset(
    {
        "api",
        "worker",
        "ai_service",
        "scheduler",
        "pipeline",
        "job",
        "run",
        "errors",
        "systemd",
        "other",
    }
)

# ConfigSummary allowlist — only these operational fields may appear.
# Secrets, credentials, and raw environment variables are never permitted.
CONFIG_SUMMARY_ALLOWED_FIELDS = frozenset(
    {
        "environment",
        "active_preset",
        "funnel",
        "platform",
        "upload",
        "scheduler",
        "schema_version",
    }
)

# Field-name tokens that must never appear on ConfigSummary or its nested dicts.
SECRET_FIELD_NAME_TOKENS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_key",
        "private_key",
        "client_secret",
        "auth",
        "credential",
        "credentials",
        "bearer",
        "cookie",
        "session_key",
        "encryption_key",
    }
)
