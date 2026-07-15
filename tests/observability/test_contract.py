"""Tests for observability contract adapters and secret safety."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
OPS_DIR = REPO_ROOT / "scripts" / "ops"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(OPS_DIR))

from observability import (  # noqa: E402
    CONTRACT_SCHEMA_VERSION,
    assert_config_summary_safe,
    config_summary_from_operational_state,
    is_secret_field_name,
    run_summary_from_run_record_dict,
)
from observability.models import (  # noqa: E402
    SchedulerStateSummary,
    UploadStateSummary,
)
import run_records as rr  # noqa: E402


class TestSecretFieldDetection:
    @pytest.mark.parametrize(
        "name",
        [
            "password",
            "api_key",
            "API_KEY",
            "client_secret",
            "access_token",
            "youtube_oauth_token",
            "db_password",
            "credentials",
        ],
    )
    def test_secret_like_names_detected(self, name: str):
        assert is_secret_field_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "environment",
            "active_preset",
            "funnel",
            "platform",
            "upload",
            "scheduler",
            "enabled",
            "status",
        ],
    )
    def test_operational_names_allowed(self, name: str):
        assert is_secret_field_name(name) is False


class TestConfigSummarySafety:
    def test_builder_rejects_secret_keys_in_upload_mapping(self):
        with pytest.raises(ValueError, match="Secret-like field"):
            config_summary_from_operational_state(
                environment="prod",
                upload={"enabled": True, "api_key": "should-not-appear"},
            )

    def test_builder_rejects_secret_keys_in_scheduler_mapping(self):
        with pytest.raises(ValueError, match="Secret-like field"):
            config_summary_from_operational_state(
                environment="prod",
                scheduler={"effective": "enabled", "token": "nope"},
            )

    def test_assert_rejects_unknown_top_level_fields(self):
        with pytest.raises(ValueError, match="non-allowlisted"):
            assert_config_summary_safe(
                {
                    "environment": "prod",
                    "active_preset": "growth",
                    "funnel": None,
                    "platform": None,
                    "upload": {},
                    "scheduler": {},
                    "schema_version": 1,
                    "raw_env": {"HOME": "/root"},
                }
            )

    def test_builder_produces_safe_payload(self):
        summary = config_summary_from_operational_state(
            environment="prod",
            active_preset="growth",
            funnel="business",
            platform="youtube",
            upload=UploadStateSummary(
                enabled=True,
                config_enabled=True,
                runtime_disabled=False,
                status="enabled",
                detail="config and runtime allow uploads",
            ),
            scheduler=SchedulerStateSummary(
                effective="enabled",
                runtime_disabled=False,
                underlying_active=True,
                mechanism="cron",
                status="enabled",
            ),
        )
        payload = summary.to_dict()
        assert_config_summary_safe(payload)
        assert payload["environment"] == "prod"
        assert payload["active_preset"] == "growth"
        assert "api_key" not in payload
        assert "password" not in str(payload).lower()


class TestRunRecordMapping:
    def test_maps_canonical_run_record_fields(self):
        record = rr.RunRecord(
            run_id="run_20260704T001200Z_manual_cli",
            environment="prod",
            trigger="manual_cli",
            status=rr.STATUS_FAIL,
            started_at="2026-07-04T00:12:00Z",
            finished_at="2026-07-04T00:12:30Z",
            duration_seconds=30.0,
            failure_reason="boot readiness NOT READY",
            jobs_started=0,
            jobs_completed=0,
            jobs_failed=0,
            log_path="runs/prod/run_20260704T001200Z_manual_cli/run.log",
            funnel_id="business",
            report_paths=["jobs/prod/job_1/report.json"],
        )
        summary = run_summary_from_run_record_dict(record.to_dict())
        assert summary.run_id == record.run_id
        assert summary.environment == "prod"
        assert summary.trigger == "manual_cli"
        assert summary.status == "FAIL"
        assert summary.duration_seconds == 30.0
        assert summary.funnel_id == "business"
        assert summary.log_path == record.log_path
        assert summary.report_paths == ["jobs/prod/job_1/report.json"]
        assert summary.failure_summary is not None
        assert summary.failure_summary.reason == "boot readiness NOT READY"
        assert summary.failure_summary.severity == "fail"
        assert (
            summary.failure_summary.suggested_next_inspection_target == record.log_path
        )
        assert summary.schema_version == CONTRACT_SCHEMA_VERSION

    def test_skipped_run_uses_warn_severity(self):
        record = {
            "run_id": "run_skip",
            "environment": "prod",
            "trigger": "scheduled",
            "status": "SKIPPED",
            "started_at": "2026-07-04T00:00:00Z",
            "finished_at": "2026-07-04T00:00:01Z",
            "failure_reason": "execution lock held",
            "log_path": "runs/prod/run_skip/run.log",
            "jobs_started": 0,
            "jobs_completed": 0,
            "jobs_failed": 0,
            "report_paths": [],
        }
        summary = run_summary_from_run_record_dict(record)
        assert summary.status == "SKIPPED"
        assert summary.failure_summary is not None
        assert summary.failure_summary.severity == "warn"

    def test_success_run_has_no_failure_summary(self):
        record = {
            "run_id": "run_ok",
            "environment": "dev",
            "trigger": "test",
            "status": "SUCCESS",
            "started_at": "2026-07-04T00:00:00Z",
            "finished_at": "2026-07-04T00:05:00Z",
            "duration_seconds": 300,
            "jobs_started": 1,
            "jobs_completed": 1,
            "jobs_failed": 0,
            "log_path": "runs/dev/run_ok/run.log",
            "report_paths": [],
        }
        summary = run_summary_from_run_record_dict(record)
        assert summary.failure_summary is None
        assert summary.jobs_completed == 1


class TestSuitabilityForSshAndUi:
    def test_models_serialize_to_plain_json_dicts(self):
        """SSH tooling and future JSON endpoints need plain dicts."""
        import json

        from observability import SystemHealth, SystemStatus

        health = SystemHealth(overall="PASS", environment="prod")
        status = SystemStatus(environment="prod", state="idle")
        # Must be JSON-serializable without custom encoders.
        health_json = json.dumps(health.to_dict())
        status_json = json.dumps(status.to_dict())
        assert "PASS" in health_json
        assert "idle" in status_json

    def test_contract_package_has_no_flask_or_ui_imports(self):
        package_dir = REPO_ROOT / "scripts" / "observability"
        forbidden_import_markers = (
            "import flask",
            "from flask",
            "import ops_ui",
            "from ops_ui",
            "import ops_ui.",
        )
        for path in package_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            lowered = text.lower()
            for marker in forbidden_import_markers:
                assert marker not in lowered, f"{path.name} contains {marker!r}"
