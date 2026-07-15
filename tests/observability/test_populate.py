"""Tests for observability populate adapters (Phase 2)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
OPS_DIR = REPO_ROOT / "scripts" / "ops"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(OPS_DIR))

from health_report import HealthCheck, HealthReport, build_health_report  # noqa: E402
from observability.models import SystemHealth, SystemStatus  # noqa: E402
from observability.populate import (  # noqa: E402
    build_system_health,
    build_system_status,
    sanitize_detail,
    services_payload,
)
from status_report import build_status_report  # noqa: E402


class TestSanitizeDetail:
    def test_redacts_absolute_paths(self):
        detail = sanitize_detail("database not readable: /var/lib/mk04/prod/db.sqlite")
        assert detail is not None
        assert "/var/lib" not in detail
        assert "[path]" in detail

    def test_redacts_secret_assignments(self):
        detail = sanitize_detail("token=abc123secretvalue api_key=xyz")
        assert detail is not None
        assert "abc123secretvalue" not in detail
        assert "[REDACTED]" in detail


class TestBuildSystemHealth:
    def test_overall_matches_ssh_health_report(self):
        report = build_health_report("dev")
        health = build_system_health("dev", report=report)
        assert health.overall == report.overall or (
            health.overall == "FAIL" and report.overall == "FAIL"
        )
        # Normalize READY-style values are not used on overall.
        assert health.overall in {"PASS", "WARN", "FAIL"}
        assert health.environment == "dev"
        assert health.boot_readiness in {"READY", "NOT READY", None}
        assert health.execution_lock is not None
        assert health.checked_at is not None
        payload = health.to_dict()
        assert "password" not in json.dumps(payload).lower()
        assert "api_key" not in json.dumps(payload).lower()

    def test_serializes_contract_model(self):
        health = build_system_health("dev")
        assert isinstance(health, SystemHealth)
        payload = health.to_dict()
        restored = SystemHealth.from_dict(payload)
        assert restored.overall == health.overall
        assert restored.environment == health.environment

    def test_inspection_failure_returns_structured_fail(self, monkeypatch: pytest.MonkeyPatch):
        import observability.populate as populate

        def _boom(_env: str) -> HealthReport:
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(populate, "build_health_report", _boom)
        health = build_system_health("dev")
        assert health.overall == "FAIL"
        assert health.readiness_failures
        assert "health inspection failed" in health.readiness_failures[0]


class TestBuildSystemStatus:
    def test_status_uses_contract_model(self):
        status = build_system_status("dev")
        assert isinstance(status, SystemStatus)
        assert status.environment == "dev"
        assert status.state in {"idle", "running", "failing", "blocked", "unknown"}
        payload = status.to_dict()
        assert payload["active_run"] is None or "run_id" in payload["active_run"]
        assert "queue" in payload
        assert "recent_summary" in payload

    def test_missing_queue_pending_is_not_fabricated_as_known(self):
        report = build_status_report("dev")
        status = build_system_status("dev", report=report)
        # Status infrastructure reports pending as not yet available.
        assert status.current_activity is not None
        assert "queue pending unknown" not in (status.current_activity or "").lower()
        assert status.queue.pending == 0

    def test_running_video_jobs_without_pipeline_run(self, monkeypatch):
        from observability.models import ExecutionLockSummary, QueueSummary
        from observability import populate

        queue = QueueSummary(pending=0, running=1, failed=0)
        state = populate._activity_state(
            active_run=None,
            lock=ExecutionLockSummary(present=False, stale=False),
            queue=queue,
        )
        assert state == "running"
        activity = populate._current_activity(
            active_run=None,
            lock=ExecutionLockSummary(present=False, stale=False),
            queue=queue,
            pending_known=False,
        )
        assert activity == "1 video job processing. No pipeline run active."


class TestServicesPayload:
    def test_services_payload_structure(self):
        payload = services_payload("dev")
        assert payload["environment"] == "dev"
        assert isinstance(payload["services"], list)
        assert payload["schema_version"] == 1
        for service in payload["services"]:
            assert "service_name" in service
            assert service["health"] in {"PASS", "WARN", "FAIL", "UNKNOWN"}
            assert service.get("restart_count") is None or isinstance(
                service["restart_count"], int
            )
            # No private absolute paths in details.
            detail = service.get("detail") or ""
            assert "/home/" not in detail
            assert "/var/lib/" not in detail
