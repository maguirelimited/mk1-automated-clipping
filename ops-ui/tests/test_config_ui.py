"""Configuration Viewer UI tests (Phase 12)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings


def _settings(tmp_path: Path, *, environment: str = "dev") -> Settings:
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
        environment=environment,
        services=(
            ServiceConfig(
                key="source-input",
                label="source-input",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-source-input.service",
            ),
        ),
    )


_VIEW = {
    "environment": "dev",
    "environment_label": "DEVELOPMENT",
    "validation": {"state": "PASS", "message": "ConfigManager load succeeded", "errors": []},
    "summary": {
        "funnel_id": "business",
        "platform_id": "youtube",
        "preset_id": "growth",
        "uploading_enabled": False,
    },
    "upload": {
        "enabled": False,
        "status": "disabled",
        "detail": "blocked by config",
        "config_enabled": False,
        "runtime_disabled": False,
    },
    "scheduler": {
        "effective": "manual",
        "status": "manual",
        "detail": "manual mode",
        "runtime_disabled": False,
        "underlying_active": None,
        "mechanism": "manual",
    },
    "system": {"max_concurrent_jobs": 1},
    "retention": {"logs_days": 30},
    "disk_pressure": {},
    "ai": {"processing_model": "placeholder-local-model"},
    "funnel": {"preset": "growth"},
    "platform": {"platform_id": "youtube", "uploading": {}, "captions": {}, "format": {}},
    "preset": {"preset_id": "growth", "selection": {}, "post_processing": {}},
    "paths": {"config_root": "config", "jobs_root": "jobs/dev"},
    "resolved_config": {"ai": {"processing_model": "placeholder-local-model"}, "api_key": "<redacted>"},
    "resolved_config_available": True,
    "schema_version": 1,
}


class TestConfigUi:
    def test_configuration_page_renders(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.config_ui.build_config_view", return_value=_VIEW):
            response = app.test_client().get("/ops/configuration")
        assert response.status_code == 200
        body = response.data
        assert b"Configuration" in body
        assert b"DEVELOPMENT" in body
        assert b"Configuration validation" in body
        assert b"Is configuration valid" in body
        assert b"Operator Console" in body
        assert b"PASS" in body
        assert b"Runtime state" in body
        assert b"Retention" in body
        assert b"AI / model configuration" in body
        assert b"Resolved configuration" in body
        assert b"Read-only" in body or b"read-only" in body
        assert b"<form" not in body.lower() or b"method=\"post\"" not in body.lower()
        assert b"type=\"submit\"" not in body.lower() or b"Apply" not in body

    def test_config_api_redacts_secrets(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch(
            "ops_ui.observability.build_config_view", return_value=_VIEW
        ):
            response = app.test_client().get("/config/current")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["data"]["validation"]["state"] == "PASS"
        assert payload["data"]["resolved_config"]["api_key"] == "<redacted>"
        assert "hunter2" not in response.get_data(as_text=True)

    def test_production_environment_visible(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path, environment="prod"))
        prod_view = dict(_VIEW)
        prod_view["environment"] = "prod"
        prod_view["environment_label"] = "PRODUCTION"
        with mock.patch("ops_ui.config_ui.build_config_view", return_value=prod_view):
            response = app.test_client().get("/ops/configuration")
        assert b"PRODUCTION" in response.data
