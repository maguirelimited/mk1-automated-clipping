"""Plain-English readiness summaries for funnel detail and sync pages."""

from __future__ import annotations

from typing import Any

from .validation import (
    FunnelValidationIssue,
    FunnelValidationReport,
    PENDING_SYNC_CODES,
    processing_blocker_issues,
    test_run_available,
)

_PLAIN_BLOCKER_MESSAGES: dict[str, str] = {
    "no_acquisition_sources": "Missing source URL",
    "missing_active_source": "No active source",
    "source_input_pending_sync": "Missing runtime sync — use Sync runtime config",
    "source_input_not_found": "Missing source-input config file on disk",
    "video_config_pending_sync": "Missing video automation config — sync first",
    "ai_registry_pending_sync": "Missing AI rule registry entry — sync first",
    "ai_prompt_pending_sync": "Missing AI prompt file — sync first",
    "config_manager_yaml_pending_sync": "Missing ConfigManager YAML — sync first",
    "missing_ai_prompt_file": "Missing AI prompt profile",
    "missing_ai_rule_alias": "Missing AI rule alias",
    "missing_pipeline_profile": "Missing pipeline profile",
    "invalid_processing_config": "Video automation config is invalid",
    "processing_funnel_id_mismatch": "Video automation config does not match this funnel",
    "missing_output_route": "Posting is enabled but no output channel is configured",
    "invalid_schema": "Funnel configuration is invalid",
}


def plain_blocker_message(issue: FunnelValidationIssue) -> str:
    return _PLAIN_BLOCKER_MESSAGES.get(issue.code, issue.message)


def processing_blocker_messages(report: FunnelValidationReport) -> list[str]:
    """Return plain-English items that block processing or a test run."""
    seen: set[str] = set()
    blockers: list[str] = []
    for issue in processing_blocker_issues(report):
        message = plain_blocker_message(issue)
        if message and message not in seen:
            seen.add(message)
            blockers.append(message)
    return blockers


def build_simple_funnel_status(
    *,
    posting_enabled: bool,
    identity_status: str,
    identity_enabled: bool,
    report: FunnelValidationReport,
    ops: dict[str, Any],
) -> dict[str, Any]:
    """Compact operator-facing status for funnel detail."""
    pending_sync = any(issue.code in PENDING_SYNC_CODES for issue in report.warnings)
    synced = report.processing_ready or (report.sync_ready and not pending_sync)
    can_run = bool(ops.get("can_run"))
    run_available = test_run_available(report=report, can_run=can_run)
    test_blocked = processing_blocker_messages(report)
    if identity_status == "draft":
        test_blocked = ["Funnel is still a draft — edit status to testing to run"] + test_blocked
    elif not identity_enabled:
        test_blocked = ["Funnel is disabled"] + test_blocked
    elif report.processing_ready and not can_run and ops.get("paused"):
        test_blocked = ["Funnel is paused"] + test_blocked

    if run_available:
        test_label = "Available"
    elif report.processing_ready and identity_enabled and identity_status in {"active", "testing"}:
        test_label = "Blocked"
    else:
        test_label = "Blocked"

    next_action: str | None = None
    if pending_sync and not report.processing_ready:
        next_action = "Sync runtime config"
    elif run_available:
        next_action = "Run test"
    elif report.processing_ready and identity_status == "draft":
        next_action = "Set status to testing"

    return {
        "created": True,
        "synced": synced,
        "synced_label": "Synced" if synced else "Not synced",
        "processing_ready": report.processing_ready,
        "processing_label": "Ready" if report.processing_ready else "Not ready",
        "test_run_label": test_label,
        "test_run_available": run_available,
        "posting_enabled": posting_enabled,
        "posting_label": "Enabled" if posting_enabled else "Disabled",
        "blockers": test_blocked,
        "next_action": next_action,
    }


def sync_outcome_message(
    *,
    applied: bool,
    sync_ok: bool,
    report: FunnelValidationReport,
    after_apply_processing_ready: bool,
) -> str:
    if not applied:
        return ""
    if not sync_ok:
        return "Sync failed — fix the errors below and try again."
    if after_apply_processing_ready:
        return "Synced successfully. Processing test can now run."
    blockers = processing_blocker_messages(report)
    if blockers:
        return f"Synced successfully. Still blocked: {blockers[0]}"
    return "Synced successfully."
