"""Canonical artifact type names for Storage & Data Management.

Inventory source of truth: docs/storage/STORAGE_INVENTORY.md

This module intentionally contains **names and static labels only**.
It does not scan disks, apply retention, or delete anything.
"""

from __future__ import annotations

# Types the classifier may emit (Phase 3 plan + inventory completeness).
ARTIFACT_TYPES = frozenset(
    {
        "source_video",
        "transcript",
        "raw_candidate_pool",
        "processing_report",
        "selection_result",
        "intermediate_render",
        "formatted_clip",
        "captioned_clip",
        "final_clip",
        "post_processing_report",
        "clip_metadata",
        "run_record",
        "job_log",
        "service_log",
        "config_snapshot",
        "database",
        "database_backup",
        "temporary_file",
        # Additional types present in the live system (documented in inventory).
        "job_report",
        "execution_context",
        "control_state",
        "input_ledger_record",
        "last_update_status",
        "pipeline_execution_lock",
        "ops_ui_controls",
        # Explicit non-match — never invent a type.
        "unknown",
    }
)

# Business outputs — never treat as default cleanup targets.
BUSINESS_OUTPUT_TYPES = frozenset(
    {
        "final_clip",
    }
)

# Explicitly temporary classes.
TEMPORARY_TYPES = frozenset(
    {
        "temporary_file",
        "intermediate_render",
    }
)

# Operational evidence (inventory classification).
OPERATIONAL_EVIDENCE_TYPES = frozenset(
    {
        "transcript",
        "raw_candidate_pool",
        "processing_report",
        "selection_result",
        "post_processing_report",
        "clip_metadata",
        "run_record",
        "job_log",
        "service_log",
        "config_snapshot",
        "database",
        "database_backup",
        "job_report",
        "execution_context",
        "control_state",
        "input_ledger_record",
        "last_update_status",
        "pipeline_execution_lock",
        "ops_ui_controls",
        "source_video",
    }
)

# Owning subsystem labels (inventory). Descriptive only.
OWNER_BY_TYPE: dict[str, str] = {
    "source_video": "source-input / video-automation",
    "transcript": "video-automation",
    "raw_candidate_pool": "video-automation / ai-service",
    "processing_report": "video-automation",
    "selection_result": "video-automation",
    "intermediate_render": "video-automation",
    "formatted_clip": "video-automation",
    "captioned_clip": "video-automation",
    "final_clip": "video-automation / output-funnel",
    "post_processing_report": "video-automation",
    "clip_metadata": "video-automation",
    "run_record": "reliability-ops",
    "job_log": "video-automation",
    "service_log": "services",
    "config_snapshot": "config-manager",
    "database": "config / output-funnel / ops-ui",
    "database_backup": "reliability-ops",
    "temporary_file": "various",
    "job_report": "video-automation",
    "execution_context": "video-automation",
    "control_state": "ops",
    "input_ledger_record": "source-input",
    "last_update_status": "update-scripts",
    "pipeline_execution_lock": "run-pipeline",
    "ops_ui_controls": "ops-ui",
    "unknown": "unknown",
}

# Observability layer uses `output_clip` for final clips in job dirs.
OBSERVABILITY_ALIASES = {
    "output_clip": "final_clip",
}

# Protection flag names (descriptive metadata only — not deletion decisions).
PROTECTION_FLAGS = frozenset(
    {
        "active_job",
        "failed_job",
        "final_clip",
        "database",
        "unknown",
        "protected_type",
    }
)
