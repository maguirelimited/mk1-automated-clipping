"""Tests for plain-English readiness summaries."""

from __future__ import annotations

from ops_ui.funnel_management.readiness_summary import (
    build_simple_funnel_status,
    plain_blocker_message,
    processing_blocker_messages,
)
from ops_ui.funnel_management.validation import (
    FunnelValidationIssue,
    FunnelValidationReport,
    FunnelValidationSeverity,
)


def _issue(code: str, message: str) -> FunnelValidationIssue:
    return FunnelValidationIssue(
        code=code,
        message=message,
        severity=FunnelValidationSeverity.ERROR,
        section="test",
        field=None,
        source=None,
    )


def test_plain_blocker_message_uses_friendly_text() -> None:
    issue = _issue("no_acquisition_sources", "No acquisition sources are configured.")
    assert plain_blocker_message(issue) == "Missing source URL"


def test_posting_blockers_do_not_affect_processing_blockers() -> None:
    report = FunnelValidationReport(
        funnel_id="test_001",
        valid_config=True,
        dependencies_ok=True,
        runnable=False,
        status="incomplete",
        sync_ready=True,
        processing_ready=True,
        posting_ready=False,
        sync_state="ready",
        processing_state="ready",
        posting_state="blocked",
        errors=(
            _issue(
                "missing_output_route",
                "Posting is enabled but no output channel routes are configured.",
            ),
        ),
        warnings=(),
        info=(),
        checked_at="2026-07-07T00:00:00Z",
    )
    assert processing_blocker_messages(report) == []
    status = build_simple_funnel_status(
        posting_enabled=True,
        identity_status="testing",
        identity_enabled=True,
        report=report,
        ops={"can_run": True, "paused": False},
    )
    assert status["processing_label"] == "Ready"
    assert status["posting_label"] == "Enabled"


def test_simple_status_shows_sync_blocker() -> None:
    report = FunnelValidationReport(
        funnel_id="test_001",
        valid_config=True,
        dependencies_ok=True,
        runnable=False,
        status="incomplete",
        sync_ready=True,
        processing_ready=False,
        posting_ready=True,
        sync_state="ready",
        processing_state="pending_sync",
        posting_state="disabled",
        errors=(),
        warnings=(
            FunnelValidationIssue(
                code="video_config_pending_sync",
                message="Video processing config is missing; sync will create test.json.",
                severity=FunnelValidationSeverity.WARNING,
                section="processing",
                field=None,
                source=None,
            ),
        ),
        info=(),
        checked_at="2026-07-07T00:00:00Z",
    )
    status = build_simple_funnel_status(
        posting_enabled=False,
        identity_status="testing",
        identity_enabled=True,
        report=report,
        ops={"can_run": False, "paused": False},
    )
    assert status["synced_label"] == "Not synced"
    assert status["next_action"] == "Sync runtime config"
    assert status["blockers"]
    assert "Missing video automation config" in status["blockers"][0]
