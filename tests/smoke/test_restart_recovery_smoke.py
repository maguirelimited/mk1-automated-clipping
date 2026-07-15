"""Tests for restart recovery smoke helper (Reliability Phase 4)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_DIR = REPO_ROOT / "scripts" / "smoke"

sys.path.insert(0, str(SMOKE_DIR))

import smoke_restart_recovery as smoke  # noqa: E402


class TestNormalizeEnv:
    def test_rejects_invalid_env(self):
        with pytest.raises(ValueError, match="invalid environment"):
            smoke.normalize_env("staging")

    def test_accepts_dev_and_prod(self):
        assert smoke.normalize_env("dev") == "dev"
        assert smoke.normalize_env("production") == "prod"


class TestCliGuards:
    def test_prod_execute_requires_confirm(self):
        code = smoke.main(["--env", "prod", "--execute", "--no-report"])
        assert code == 2

    def test_invalid_env_exits_2(self):
        code = smoke.main(["--env", "staging", "--no-report"])
        assert code == 2

    def test_policy_only_passes_on_unit_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(smoke, "write_report", lambda report, env: tmp_path / "r.json")
        monkeypatch.setattr(smoke, "systemctl_available", lambda: False)
        code = smoke.main(["--env", "dev", "--no-report"])
        assert code == 0


class TestUnitFilePolicy:
    def test_all_core_units_declare_restart_always_sec_5(self):
        for service in smoke.CORE_SERVICES:
            path = smoke.unit_file_path(service["unit"])
            restart, sec, problems = smoke.parse_unit_restart_policy(path)
            assert problems == [], (service["unit"], problems)
            assert restart == "always"
            assert sec == 5

    def test_parse_detects_wrong_policy(self, tmp_path: Path):
        path = tmp_path / "bad.service"
        path.write_text(
            "[Service]\nRestart=on-failure\nRestartSec=30\n[Install]\nWantedBy=multi-user.target\n",
            encoding="utf-8",
        )
        restart, sec, problems = smoke.parse_unit_restart_policy(path)
        assert restart == "on-failure"
        assert sec == 30
        assert problems


class TestParseRestartUsec:
    def test_seconds_and_microseconds(self):
        assert smoke.parse_restart_usec("5s") == 5
        assert smoke.parse_restart_usec("5000000") == 5
        assert smoke.parse_restart_usec("5000ms") == 5


class TestOverall:
    def test_fail_beats_warn(self):
        checks = [
            smoke.CheckResult("a", "PASS"),
            smoke.CheckResult("b", "WARN"),
            smoke.CheckResult("c", "FAIL"),
        ]
        assert smoke.overall_from_checks(checks) == "FAIL"

    def test_skip_is_warn(self):
        checks = [
            smoke.CheckResult("a", "PASS"),
            smoke.CheckResult("b", "SKIP"),
        ]
        assert smoke.overall_from_checks(checks) == "WARN"


class TestExecuteRecoveryLogic:
    def test_kill_failure_stops_recovery(self, monkeypatch: pytest.MonkeyPatch):
        service = smoke.CORE_SERVICES[1]  # api
        monkeypatch.setattr(smoke, "systemctl_is_active", lambda _u: "active")
        monkeypatch.setattr(smoke, "systemctl_show", lambda _u, prop: "1" if prop == "NRestarts" else "99")

        def kill_fn(_unit: str):
            return False, "permission denied", None

        results = smoke.execute_recovery_for_service(
            "dev",
            service,
            kill_fn=kill_fn,
        )
        assert results[0].name == "kill:api"
        assert results[0].outcome == "FAIL"
        assert len(results) == 1

    def test_recovery_requires_nrestarts_increase(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(smoke, "systemctl_is_active", lambda _u: "active")
        monkeypatch.setattr(smoke, "systemctl_show", lambda _u, prop: "3" if prop == "NRestarts" else "1")

        result = smoke.wait_for_recovery(
            "mk04-source-input.service",
            previous_nrestarts=3,
            health_url="http://127.0.0.1:5160/healthz",
            probe_fn=lambda _url: (True, "HTTP 200"),
            wait_seconds=0.3,
        )
        assert result.outcome == "FAIL"
        assert "NRestarts did not increase" in result.detail

    def test_execute_recovery_success_path(self, monkeypatch: pytest.MonkeyPatch):
        service = {"mode": "api", "unit": "mk04-source-input.service", "label": "API"}
        monkeypatch.setattr(smoke, "systemctl_is_active", lambda _u: "active")
        monkeypatch.setattr(smoke, "systemctl_show", lambda _u, prop: "2" if prop == "NRestarts" else "1234")

        def kill_fn(_unit: str):
            return True, "SIGKILL sent to pid 1234", 1234

        def wait_fn(unit, **kwargs):
            return smoke.CheckResult(
                name=f"recover:{unit}",
                outcome="PASS",
                detail="active again; NRestarts 2→3; health HTTP 200",
                meta={"nrestarts_before": 2, "nrestarts_after": 3},
            )

        monkeypatch.setattr(
            smoke,
            "journal_restart_visible",
            lambda unit, since_iso: smoke.CheckResult(
                name=f"journal:{unit}",
                outcome="PASS",
                detail="journal entries present",
            ),
        )

        results = smoke.execute_recovery_for_service(
            "dev",
            service,
            kill_fn=kill_fn,
            wait_fn=wait_fn,
        )
        outcomes = {r.name: r.outcome for r in results}
        assert outcomes["kill:api"] == "PASS"
        assert outcomes["recover:api"] == "PASS"
        assert outcomes["journal:mk04-source-input.service"] == "PASS"

    def test_services_for_run_optional_funnel(self):
        base = smoke.services_for_run(include_output_funnel=False)
        with_funnel = smoke.services_for_run(include_output_funnel=True)
        assert len(with_funnel) == len(base) + 1
        assert any(s["mode"] == "output-funnel" for s in with_funnel)
