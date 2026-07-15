"""Tests for Storage Safety & Integration smoke CLI (Phase 12)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_DIR = REPO_ROOT / "scripts" / "smoke"
sys.path.insert(0, str(SMOKE_DIR))

import smoke_storage as smoke  # noqa: E402


class TestNormalizeEnv:
    def test_rejects_invalid(self):
        with pytest.raises(ValueError):
            smoke.normalize_env("staging")

    def test_accepts_dev_prod(self):
        assert smoke.normalize_env("dev") == "dev"
        assert smoke.normalize_env("production") == "prod"

    def test_none_allowed(self):
        assert smoke.normalize_env(None) is None


class TestStaticChecks:
    def test_modules_present(self):
        results = smoke.check_storage_modules_present()
        assert all(r.outcome == "PASS" for r in results)

    def test_ops_entrypoints_present(self):
        results = smoke.check_ops_entrypoints_present()
        assert all(r.outcome == "PASS" for r in results)

    def test_storage_ui_read_only(self):
        result = smoke.check_storage_ui_read_only()
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
