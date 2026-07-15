"""Tests for Reliability & Recovery smoke helper (Phase 11)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_DIR = REPO_ROOT / "scripts" / "smoke"
sys.path.insert(0, str(SMOKE_DIR))

import smoke_reliability as smoke  # noqa: E402


class TestNormalizeEnv:
    def test_rejects_invalid(self):
        with pytest.raises(ValueError):
            smoke.normalize_env("staging")

    def test_accepts_dev_prod(self):
        assert smoke.normalize_env("dev") == "dev"
        assert smoke.normalize_env("production") == "prod"


class TestStaticChecks:
    def test_scripts_present(self):
        results = smoke.check_scripts_present()
        assert results
        assert all(r.outcome == "PASS" for r in results)

    def test_unit_policies(self):
        results = smoke.check_unit_restart_policy()
        assert all(r.outcome == "PASS" for r in results)

    def test_cron_entrypoint(self):
        result = smoke.check_cron_uses_shared_entrypoint()
        assert result.outcome == "PASS"


class TestOverall:
    def test_fail_beats_warn(self):
        checks = [
            smoke.CheckResult("a", "PASS"),
            smoke.CheckResult("b", "SKIP"),
            smoke.CheckResult("c", "FAIL"),
        ]
        assert smoke.overall_from_checks(checks) == "FAIL"


class TestCli:
    def test_invalid_env(self):
        assert smoke.main(["--env", "staging", "--no-report"]) == 2
