"""Tests for scripts/ops/health_report.py service probes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
OPS_DIR = REPO_ROOT / "scripts" / "ops"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(OPS_DIR))

import health_report as hr  # noqa: E402


class TestServiceHealthHttpFirst:
    def test_worker_passes_when_http_ok_despite_systemd_inactive(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            hr,
            "service_health_urls",
            lambda _env: {"Worker": "http://127.0.0.1:5150/healthz"},
        )
        monkeypatch.setattr(hr, "http_probe", lambda _url: (True, "HTTP 200"))
        monkeypatch.setattr(
            hr,
            "systemd_unit_status",
            lambda _unit: ("FAIL", "systemd reports inactive", "fail"),
        )

        check = hr._service_health("Worker", "mk04-video-automation.service", "dev")

        assert check.result == "PASS"
        assert "HTTP 200" in check.detail

    def test_worker_fails_when_http_unreachable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            hr,
            "service_health_urls",
            lambda _env: {"Worker": "http://127.0.0.1:5150/healthz"},
        )
        monkeypatch.setattr(
            hr,
            "http_probe",
            lambda _url: (False, "[Errno 111] Connection refused"),
        )
        monkeypatch.setattr(
            hr,
            "systemd_unit_status",
            lambda _unit: ("FAIL", "systemd reports inactive", "fail"),
        )

        check = hr._service_health("Worker", "mk04-video-automation.service", "dev")

        assert check.result == "FAIL"
        assert check.severity == "fail"

    def test_optional_ai_service_warns_when_unreachable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            hr,
            "service_health_urls",
            lambda _env: {"AI service": "http://127.0.0.1:5175/health"},
        )
        monkeypatch.setattr(hr, "http_probe", lambda _url: (False, "[Errno 111] Connection refused"))
        monkeypatch.setattr(
            hr,
            "systemd_unit_status",
            lambda _unit: ("FAIL", "systemd reports inactive", "fail"),
        )

        check = hr._service_health("AI service", "mk04-ai-service.service", "dev")

        assert check.result == "WARN"
        assert check.severity == "warn"

    def test_ops_ui_passes_on_http_401(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            hr,
            "service_health_urls",
            lambda _env: {"Operations UI": "http://127.0.0.1:5170/health"},
        )
        monkeypatch.setattr(hr, "http_probe", lambda _url: (False, "HTTP 401"))
        monkeypatch.setattr(
            hr,
            "systemd_unit_status",
            lambda _unit: ("FAIL", "systemd reports inactive", "fail"),
        )

        check = hr._service_health("Operations UI", "mk04-ops-ui.service", "dev")

        assert check.result == "PASS"
        assert "authentication required" in check.detail


class TestBootReadinessExit:
    def test_ready_exit_0(self):
        from boot_verification import BootComponent, BootVerification

        report = hr.HealthReport(
            env_label="PRODUCTION",
            mk04_env="prod",
            is_production=True,
            boot=BootVerification(
                environment="production",
                env_label="PRODUCTION",
                overall="READY",
                components=[BootComponent("API", "PASS", "ok", True)],
            ),
            overall="WARN",
            checks=[hr.HealthCheck("Upload safety state", "WARN", "uploads disabled")],
        )
        assert hr._boot_readiness_exit_code(report) == 0

    def test_ready_with_optional_warn_exit_1(self):
        from boot_verification import BootComponent, BootVerification

        report = hr.HealthReport(
            env_label="PRODUCTION",
            mk04_env="prod",
            is_production=True,
            boot=BootVerification(
                environment="production",
                env_label="PRODUCTION",
                overall="READY",
                components=[
                    BootComponent("API", "PASS", "ok", True),
                    BootComponent("AI service", "WARN", "optional", False),
                ],
            ),
            overall="WARN",
            checks=[],
        )
        assert hr._boot_readiness_exit_code(report) == 1

    def test_not_ready_exit_2(self):
        from boot_verification import BootComponent, BootVerification

        report = hr.HealthReport(
            env_label="PRODUCTION",
            mk04_env="prod",
            is_production=True,
            boot=BootVerification(
                environment="production",
                env_label="PRODUCTION",
                overall="NOT READY",
                components=[BootComponent("API", "FAIL", "down", True)],
            ),
            overall="FAIL",
            checks=[],
        )
        assert hr._boot_readiness_exit_code(report) == 2

    def test_missing_boot_fails_closed(self):
        report = hr.HealthReport(
            env_label="PRODUCTION",
            mk04_env="prod",
            is_production=True,
            boot=None,
            overall="PASS",
            checks=[],
        )
        assert hr._boot_readiness_exit_code(report) == 2
