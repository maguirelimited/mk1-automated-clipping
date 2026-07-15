"""Tests for Operations & Observability smoke helper (Phase 15)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_DIR = REPO_ROOT / "scripts" / "smoke"
sys.path.insert(0, str(SMOKE_DIR))

import smoke_observability as smoke  # noqa: E402


class TestNormalizeEnv:
    def test_rejects_invalid(self):
        with pytest.raises(ValueError):
            smoke.normalize_env("staging")

    def test_accepts_dev_prod(self):
        assert smoke.normalize_env("dev") == "dev"
        assert smoke.normalize_env("production") == "prod"


class TestOverall:
    def test_fail_beats_warn(self):
        checks = [
            smoke.CheckResult("a", "PASS"),
            smoke.CheckResult("b", "WARN"),
            smoke.CheckResult("c", "FAIL"),
        ]
        assert smoke.overall_from_checks(checks) == "FAIL"

    def test_warn_when_no_fail(self):
        checks = [
            smoke.CheckResult("a", "PASS"),
            smoke.CheckResult("b", "WARN"),
        ]
        assert smoke.overall_from_checks(checks) == "WARN"


class TestEndToEndSmoke:
    def test_operator_workflow_dev(self):
        report = smoke.build_report("dev")
        assert report.checks
        failed = [c for c in report.checks if c["outcome"] == "FAIL"]
        assert not failed, failed
        names = {c["name"] for c in report.checks}
        for required in (
            "unauthenticated_redirect",
            "login",
            "overview_http",
            "health_http",
            "health_json",
            "config_page",
            "controls_forms",
            "controls_high_risk_confirm",
            "logout",
            "post_logout_protection",
            "static_css",
        ):
            assert required in names
        assert report.overall in {"PASS", "WARN"}

    def test_cli_invalid_env(self):
        assert smoke.main(["--env", "staging", "--no-report"]) == 2

    def test_cli_dev_exits_zero_when_pass_or_warn(self):
        code = smoke.main(["--env", "dev", "--no-report"])
        assert code in {0, 1}
