"""Tests for Remote Operations smoke helper (Prompt 11)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_DIR = REPO_ROOT / "scripts" / "smoke"

sys.path.insert(0, str(SMOKE_DIR))

import smoke_remote_operations as smoke  # noqa: E402


class TestNormalizeEnv:
    def test_rejects_invalid_env(self):
        with pytest.raises(ValueError, match="invalid environment"):
            smoke.normalize_env("staging")

    def test_accepts_dev_and_prod(self):
        assert smoke.normalize_env("dev") == "dev"
        assert smoke.normalize_env("production") == "prod"


class TestCliGuards:
    def test_prod_requires_safe_only(self):
        code = smoke.main(["--env", "prod"])
        assert code == 2

    def test_invalid_env_exits_2(self):
        code = smoke.main(["--env", "staging"])
        assert code == 2


class TestExpectedRefusal:
    def test_dangerous_prod_commands_refusing_are_expected(self):
        result = smoke.CommandResult(
            name="guard",
            command=["enable-uploads.sh", "prod"],
            exit_code=1,
            outcome="FAIL",
            stdout_excerpt="Refusing to enable production uploads without --confirm.",
        )
        classified = smoke.classify_expected_refusal(
            result, markers=("--confirm", "refusing", "confirm")
        )
        assert classified.outcome == "EXPECTED_REFUSAL"

    def test_success_is_not_expected_refusal(self):
        result = smoke.CommandResult(
            name="guard",
            command=["enable-uploads.sh", "prod"],
            exit_code=0,
            outcome="PASS",
            stdout_excerpt="Upload control updated",
        )
        classified = smoke.classify_expected_refusal(
            result, markers=("--confirm", "refusing", "confirm")
        )
        assert classified.outcome == "FAIL"


class TestHealthNonZero:
    def test_health_warn_does_not_fail_overall(self):
        results = [
            smoke.CommandResult("status", ["status"], 0, "PASS"),
            smoke.classify_health(smoke.CommandResult("health", ["health"], 1, "FAIL")),
        ]
        assert smoke.overall_from_results(results) == "WARN"

    def test_health_fail_exit_is_warn(self):
        result = smoke.classify_health(smoke.CommandResult("health", ["health"], 2, "FAIL"))
        assert result.outcome == "WARN"


class TestProdSafeOnlyDoesNotMutate:
    def test_prod_safe_only_command_list_has_no_mutating_confirmed_commands(self):
        captured: list[list[str]] = []

        def fake_run(name: str, args: list[str], *, timeout: float = 120.0):
            captured.append(args)
            joined = " ".join(args)
            # Simulate confirmation guards refusing.
            if "enable-uploads.sh" in joined and "prod" in args and "--confirm" not in args:
                return smoke.CommandResult(name, args, 1, "FAIL", stdout_excerpt="--confirm required")
            if "start-scheduler.sh" in joined and "prod" in args and "--confirm" not in args:
                return smoke.CommandResult(name, args, 1, "FAIL", stdout_excerpt="--confirm required")
            if "restart.sh" in joined and "all" in args and "--confirm" not in args:
                return smoke.CommandResult(name, args, 1, "FAIL", stdout_excerpt="--confirm required")
            if "cleanup.sh" in joined and "--apply" in args:
                return smoke.CommandResult(name, args, 1, "FAIL", stdout_excerpt="not implemented retention")
            return smoke.CommandResult(name, args, 0, "PASS", stdout_excerpt="Environment: PRODUCTION No files deleted.")

        with mock.patch.object(smoke, "run_command", side_effect=fake_run):
            with mock.patch.object(smoke, "check_scripts_exist", return_value=[]):
                report = smoke.run_smoke("prod", safe_only=True)

        joined_commands = [" ".join(args) for args in captured]
        forbidden_substrings = [
            "enable-uploads.sh prod --confirm",
            "start-scheduler.sh prod --confirm",
            "restart.sh prod all --confirm",
            "disable-uploads.sh prod",
            "stop-scheduler.sh prod",
            "backup.sh prod",
        ]
        for forbidden in forbidden_substrings:
            assert not any(forbidden in cmd for cmd in joined_commands), forbidden

        # Real worker restart is forbidden; dry-run is allowed.
        worker_restarts = [cmd for cmd in joined_commands if "restart.sh" in cmd and "worker" in cmd]
        assert worker_restarts
        assert all("--dry-run" in cmd for cmd in worker_restarts)

        # Guards must have been attempted without confirm.
        assert any("enable-uploads.sh" in cmd and "prod" in cmd for cmd in joined_commands)
        assert any("start-scheduler.sh" in cmd and "prod" in cmd for cmd in joined_commands)
        assert any("cleanup.sh" in cmd and "--apply" in cmd for cmd in joined_commands)
        assert report.overall in {"PASS", "WARN"}
        outcomes = {item["outcome"] for item in report.commands}
        assert "EXPECTED_REFUSAL" in outcomes


class TestReportWriting:
    def test_write_report(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(smoke, "REPO_ROOT", tmp_path)
        report = smoke.SmokeReport(
            environment="dev",
            safe_only=True,
            started_at="2026-07-03T19:00:00Z",
            finished_at="2026-07-03T19:00:01Z",
            commands=[{"name": "status", "outcome": "PASS", "exit_code": 0}],
            overall="PASS",
        )
        path = smoke.write_report(report, "dev")
        assert path.is_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["environment"] == "dev"
        assert payload["safe_only"] is True
        assert payload["overall"] == "PASS"
        latest = tmp_path / "reports" / "dev" / "remote_operations_smoke" / "latest.json"
        assert latest.is_file()
