"""Ops UI observability JSON endpoints (Phase 2)."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
OPS_DIR = REPO_ROOT / "scripts" / "ops"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(OPS_DIR))

from health_report import build_health_report  # noqa: E402
from observability.envelope import API_ENVELOPE_SCHEMA_VERSION  # noqa: E402
from observability.models import SystemHealth, SystemStatus  # noqa: E402


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=5070,
        data_dir=tmp_path,
        control_db_path=tmp_path / "ops.sqlite3",
        controls_file=tmp_path / "controls.json",
        service_timeout_sec=0.01,
        journal_lines=1,
        funnel_run_timeout_sec=1.0,
        stuck_running_sec=7200.0,
        stuck_queued_sec=1800.0,
        stuck_uploading_sec=1800.0,
        environment="dev",
        runtime_root=tmp_path / "runtime",
        services=(
            ServiceConfig(
                key="source-input",
                label="source-input",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-source-input.service",
            ),
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-video-automation.service",
            ),
            ServiceConfig(
                key="output-funnel",
                label="output-funnel",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-output-funnel.service",
            ),
        ),
    )


def _assert_no_secrets(payload: object) -> None:
    text = json.dumps(payload).lower()
    for token in ("password", "api_key", "apikey", "client_secret", "bearer "):
        assert token not in text


def _unwrap(response_json: dict) -> dict:
    assert response_json["schema_version"] == API_ENVELOPE_SCHEMA_VERSION
    assert isinstance(response_json["generated_at"], str)
    assert response_json["generated_at"].endswith("Z")
    assert isinstance(response_json["data"], dict)
    return response_json["data"]


class TestObservabilityEndpoints:
    def test_health_returns_valid_contract_json(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        response = app.test_client().get("/health")
        assert response.status_code == 200
        envelope = response.get_json()
        assert envelope is not None
        payload = _unwrap(envelope)
        health = SystemHealth.from_dict(payload)
        assert health.overall in {"PASS", "WARN", "FAIL"}
        assert health.environment == "dev"
        assert "upload" in payload
        assert "scheduler" in payload
        assert "execution_lock" in payload
        assert "disk" in payload
        assert "services" in payload
        assert "readiness_failures" in payload
        _assert_no_secrets(envelope)

    def test_status_returns_valid_contract_json(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        response = app.test_client().get("/status")
        assert response.status_code == 200
        envelope = response.get_json()
        assert envelope is not None
        payload = _unwrap(envelope)
        status = SystemStatus.from_dict(payload)
        assert status.environment == "dev"
        assert status.state in {"idle", "running", "failing", "blocked", "unknown"}
        assert "queue" in payload
        assert "recent_summary" in payload
        _assert_no_secrets(envelope)

    def test_services_returns_valid_json(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        response = app.test_client().get("/services")
        assert response.status_code == 200
        envelope = response.get_json()
        assert envelope is not None
        payload = _unwrap(envelope)
        assert payload["environment"] == "dev"
        assert isinstance(payload["services"], list)
        for service in payload["services"]:
            assert "service_name" in service
            assert "health" in service
            assert "state" in service
        _assert_no_secrets(envelope)

    def test_health_overall_agrees_with_ssh_health_layer(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        response = app.test_client().get("/health")
        assert response.status_code == 200
        payload = _unwrap(response.get_json())
        report = build_health_report("dev")
        assert payload["overall"] == report.overall

    def test_endpoints_do_not_return_500_on_normal_degraded_state(
        self, tmp_path: Path
    ) -> None:
        app = create_app(_settings(tmp_path))
        for path in ("/health", "/status", "/services", "/runs", "/jobs"):
            response = app.test_client().get(path)
            assert response.status_code == 200
            assert response.is_json
            _unwrap(response.get_json())

    def test_runs_and_jobs_list_endpoints(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        runs = app.test_client().get("/runs")
        assert runs.status_code == 200
        runs_data = _unwrap(runs.get_json())
        assert runs_data["environment"] == "dev"
        assert isinstance(runs_data["runs"], list)

        jobs = app.test_client().get("/jobs")
        assert jobs.status_code == 200
        jobs_data = _unwrap(jobs.get_json())
        assert jobs_data["environment"] == "dev"
        assert isinstance(jobs_data["jobs"], list)
        _assert_no_secrets(runs.get_json())
        _assert_no_secrets(jobs.get_json())

    def test_missing_run_and_job_return_structured_404(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        run_resp = app.test_client().get("/runs/run_does_not_exist")
        assert run_resp.status_code == 404
        run_body = run_resp.get_json()
        assert run_body["data"] is None
        assert run_body["error"]["code"] == "not_found"
        assert run_body["error"]["resource"] == "run"

        job_resp = app.test_client().get("/jobs/job_does_not_exist")
        assert job_resp.status_code == 404
        job_body = job_resp.get_json()
        assert job_body["data"] is None
        assert job_body["error"]["code"] == "not_found"
        assert job_body["error"]["resource"] == "job"

        artifacts_resp = app.test_client().get("/jobs/job_does_not_exist/artifacts")
        assert artifacts_resp.status_code == 404
        artifacts_body = artifacts_resp.get_json()
        assert artifacts_body["data"] is None
        assert artifacts_body["error"]["code"] == "not_found"
        assert artifacts_body["error"]["resource"] == "job"

    def test_log_endpoints_return_enveloped_payloads(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        for path in (
            "/logs/api",
            "/logs/worker",
            "/logs/ai",
            "/logs/scheduler",
            "/logs/errors",
        ):
            response = app.test_client().get(path)
            assert response.status_code == 200
            payload = _unwrap(response.get_json())
            assert payload["environment"] == "dev"
            assert "entries" in payload
            assert payload["limit"] <= 1000
            assert payload["status"] in {"ok", "empty", "unavailable"}
            _assert_no_secrets(response.get_json())

        missing = app.test_client().get("/jobs/job_missing/logs")
        assert missing.status_code == 404
        assert missing.get_json()["error"]["code"] == "not_found"

    def test_job_artifacts_endpoint_for_existing_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jobs_root = tmp_path / "jobs"
        job_id = "job_20260101T120000Z_artifact1"
        job_dir = jobs_root / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "report.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "status": "success",
                    "environment": "development",
                }
            ),
            encoding="utf-8",
        )
        (job_dir / "transcript.json").write_text("{}", encoding="utf-8")
        (job_dir / "job.log").write_text("log\n", encoding="utf-8")

        runtime_root = tmp_path / "runtime"
        runtime_root.mkdir(exist_ok=True)
        monkeypatch.delenv("MK04_RUNTIME_ROOT", raising=False)
        monkeypatch.setenv("MK04_JOBS_ROOT", str(jobs_root))
        monkeypatch.setenv("MK04_ENV", "dev")

        settings = replace(_settings(tmp_path), runtime_root=runtime_root)
        app = create_app(settings)

        response = app.test_client().get(f"/jobs/{job_id}/artifacts")
        assert response.status_code == 200
        payload = _unwrap(response.get_json())
        assert payload["environment"] == "dev"
        assert payload["job_id"] == job_id
        assert isinstance(payload["artifacts"], list)
        assert payload["count"] == len(payload["artifacts"])
        types = {item["artifact_type"] for item in payload["artifacts"]}
        assert "transcript" in types
        assert "job_log" in types
        for item in payload["artifacts"]:
            if item.get("path"):
                assert not item["path"].startswith("/")
                assert item["path"].startswith("jobs/dev/")
        _assert_no_secrets(response.get_json())
        assert "/var/lib/mk04" not in str(jobs_root)
