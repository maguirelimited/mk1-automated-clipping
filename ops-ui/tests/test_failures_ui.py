"""Failures page UI tests (Phase 11)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings


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
        services=(
            ServiceConfig(
                key="source-input",
                label="source-input",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-source-input.service",
            ),
        ),
    )


_GROUP = {
    "group_key": "pipeline_stage:captions",
    "category": "Pipeline Stage",
    "name": "captions",
    "count": 2,
    "severity": "ERROR",
    "latest_occurrence": "2026-07-04T00:02:00Z",
    "representative_reason": "Missing transcript segment",
    "suggested_next_inspection_target": "Inspect post_processing_report.json",
    "affected_jobs": ["job_a", "job_b"],
    "affected_runs": [],
}


class TestFailuresUi:
    def test_failures_page_renders_groups(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch(
            "ops_ui.failures_ui.failures_payload",
            return_value={
                "total_failures": 2,
                "failed_jobs": 2,
                "failed_runs": 0,
                "distinct_groups": 1,
                "groups": [_GROUP],
            },
        ):
            response = app.test_client().get("/ops/failures")
        assert response.status_code == 200
        body = response.data
        assert b"Failures" in body
        assert b"Total failures" in body
        assert b"captions" in body
        assert b"Missing transcript segment" in body
        assert b"Failure groups" in body
        assert b"Inspect" in body

    def test_empty_failures(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch(
            "ops_ui.failures_ui.failures_payload",
            return_value={
                "total_failures": 0,
                "failed_jobs": 0,
                "failed_runs": 0,
                "distinct_groups": 0,
                "groups": [],
            },
        ):
            response = app.test_client().get("/ops/failures")
        assert response.status_code == 200
        assert b"No failures recorded right now" in response.data
        assert b"Operator Console" in response.data

    def test_failures_page_loop_links(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch(
            "ops_ui.failures_ui.failures_payload",
            return_value={
                "total_failures": 1,
                "failed_jobs": 1,
                "failed_runs": 0,
                "distinct_groups": 1,
                "groups": [_GROUP],
            },
        ):
            response = app.test_client().get("/ops/failures")
        body = response.data
        assert b"What failed" in body
        assert b"Operator Console" in body
        assert b"/ops/jobs?state=failed" in body

    def test_failure_group_page(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch(
            "ops_ui.failures_ui.failure_group_payload",
            return_value={
                "group": _GROUP,
                "related_jobs": [
                    {"job_id": "job_a", "path": "/ops/jobs/job_a"},
                    {"job_id": "job_b", "path": "/ops/jobs/job_b"},
                ],
                "related_runs": [],
            },
        ):
            response = app.test_client().get("/ops/failures/pipeline_stage:captions")
        assert response.status_code == 200
        assert b"captions" in response.data
        assert b"Operator Console" in response.data
        assert b"/ops/jobs/job_a" in response.data
        assert b"Inspect post_processing_report.json" in response.data

    def test_failures_api(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch(
            "ops_ui.observability.failures_payload",
            return_value={
                "environment": "dev",
                "total_failures": 1,
                "failed_jobs": 1,
                "failed_runs": 0,
                "distinct_groups": 1,
                "groups": [_GROUP],
                "schema_version": 1,
            },
        ):
            response = app.test_client().get("/failures")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["data"]["total_failures"] == 1
        assert payload["data"]["groups"][0]["name"] == "captions"
