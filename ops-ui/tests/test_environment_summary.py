"""Tests for read-only environment/config summary (Prompt 7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.environment_summary import (
    banner_text,
    build_environment_summary,
    load_job_execution_context,
    normalize_mk04_env,
    redact_dict,
)


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
                key="video-automation",
                label="video-automation",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-video-automation.service",
            ),
        ),
    )


class TestNormalizeMk04Env:
    def test_dev_maps_to_development(self) -> None:
        env, err = normalize_mk04_env("dev")
        assert env == "development"
        assert err is None

    def test_prod_maps_to_production(self) -> None:
        env, err = normalize_mk04_env("prod")
        assert env == "production"
        assert err is None

    def test_missing_defaults_to_development(self) -> None:
        env, err = normalize_mk04_env(None)
        assert env == "development"
        assert err is None

    def test_invalid_env_returns_error(self) -> None:
        env, err = normalize_mk04_env("staging")
        assert env is None
        assert err is not None
        assert "Invalid MK04_ENV" in err


class TestBuildEnvironmentSummary:
    def test_dev_summary(self, tmp_path: Path) -> None:
        summary = build_environment_summary(_settings(tmp_path, environment="dev"))
        assert summary["environment"] == "development"
        assert summary["environment_label"] == "DEVELOPMENT"
        assert summary["is_production"] is False
        assert summary["funnel_id"] == "business"
        assert summary["platform_id"] == "youtube"
        assert summary["config_validation_state"] == "pass"
        assert summary["runtime_upload_control_available"] is True
        assert "Runtime uploads:" in summary["runtime_upload_control_label"]
        assert "jobs_root" in summary
        assert summary["jobs_root"] != "not_available"
        assert summary["boot_readiness"] in {"READY", "NOT READY", "unknown"}
        assert summary["health_state"] in {"ready", "not_ready", "unknown"}
        assert isinstance(summary.get("boot_components"), list)

    def test_prod_summary(self, tmp_path: Path) -> None:
        summary = build_environment_summary(_settings(tmp_path, environment="prod"))
        assert summary["environment"] == "production"
        assert summary["environment_label"] == "PRODUCTION"
        assert summary["is_production"] is True

    def test_invalid_env_summary(self, tmp_path: Path) -> None:
        summary = build_environment_summary(_settings(tmp_path, environment="bad-env"))
        assert summary["environment"] == "invalid"
        assert summary["config_validation_state"] == "fail"
        assert summary["error"]

    def test_posting_config_follows_config_manager(self, tmp_path: Path) -> None:
        # Prod YAML keeps uploading.enabled false until deliberately armed.
        dev = build_environment_summary(_settings(tmp_path, environment="dev"))
        prod = build_environment_summary(_settings(tmp_path, environment="prod"))
        assert dev["posting_config_enabled"] is False
        assert prod["posting_config_enabled"] is False
        assert "Posting config:" in dev["posting_config_label"]
        assert "Posting config: disabled" in prod["posting_config_label"]

    def test_runtime_upload_control_uses_ops_readonly_loader(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _disabled(_data_root: Path) -> tuple[bool | None, str]:
            return True, "test detail from canonical loader"

        monkeypatch.setattr(
            "ops_readonly.load_runtime_upload_control",
            _disabled,
        )
        summary = build_environment_summary(_settings(tmp_path, environment="dev"))
        assert summary["runtime_upload_control_label"] == (
            "Runtime uploads: DISABLED (kill switch active)"
        )
        assert summary.get("runtime_upload_control_detail") == "test detail from canonical loader"

    def test_no_secrets_in_summary(self, tmp_path: Path) -> None:
        summary = build_environment_summary(_settings(tmp_path, environment="dev"))
        blob = json.dumps(summary).lower()
        assert "api_key" not in blob
        assert "password" not in blob
        assert "secret" not in blob or "[redacted]" in blob


class TestRedaction:
    def test_redacts_secret_keys(self) -> None:
        data = {"api_key": "sk-live-abc", "funnel_id": "business"}
        redacted = redact_dict(data)
        assert redacted["api_key"] == "[REDACTED]"
        assert redacted["funnel_id"] == "business"

    def test_redacts_env_like_values(self) -> None:
        redacted = redact_dict({"token": "Bearer abcdef"})
        assert redacted["token"] == "[REDACTED]"


class TestJobExecutionContext:
    def test_loads_from_execution_context_json(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_abc"
        job_dir.mkdir()
        ctx = {
            "environment": "development",
            "job_id": "job_abc",
            "funnel_id": "business",
            "platform_id": "youtube",
            "preset_id": "growth",
            "code_commit": "abc123",
            "resolved_config_path": str(job_dir / "resolved_config.yaml"),
            "api_secret": "must-not-leak",
        }
        (job_dir / "execution_context.json").write_text(json.dumps(ctx))
        loaded = load_job_execution_context("job_abc", str(tmp_path))
        assert loaded is not None
        assert loaded["funnel_id"] == "business"
        assert loaded["api_secret"] == "[REDACTED]"


class TestUiRendering:
    def test_development_banner_on_dashboard(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path, environment="dev"))
        response = app.test_client().get("/", follow_redirects=True)
        assert response.status_code == 200
        body = response.data
        assert b"DEVELOPMENT" in body
        assert b"env-development" in body
        assert b"Health" in body
        assert b"Upload" in body
        assert b"Scheduler" in body

    def test_production_banner(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path, environment="prod"))
        response = app.test_client().get("/", follow_redirects=True)
        assert response.status_code == 200
        body = response.data
        assert b"PRODUCTION" in body
        assert b"env-production" in body
        assert b"DEVELOPMENT" not in body

    def test_api_environment_read_only(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path, environment="dev"))
        client = app.test_client()
        for path in ("/api/environment", "/api/config-summary"):
            response = client.get(path)
            assert response.status_code == 200
            data = response.get_json()
            assert data["environment_label"] == "DEVELOPMENT"
            assert data["config_validation_state"] == "pass"
            assert "password" not in json.dumps(data).lower()

    def test_no_mutating_environment_endpoints(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path, environment="dev"))
        client = app.test_client()
        for method, path in (
            ("POST", "/api/environment"),
            ("PATCH", "/api/environment"),
            ("DELETE", "/api/environment"),
            ("POST", "/api/config-summary"),
        ):
            response = client.open(path, method=method)
            assert response.status_code in {404, 405}


class TestBannerText:
    def test_banner_includes_profile(self, tmp_path: Path) -> None:
        summary = build_environment_summary(_settings(tmp_path, environment="dev"))
        text = banner_text(summary)
        assert "DEVELOPMENT" in text
        assert "business" in text
        assert "youtube" in text


class TestLastUpdateStatus:
    def test_load_last_update_status_redacts_secrets(self, tmp_path: Path) -> None:
        from ops_ui.environment_summary import load_last_update_status

        data_root = tmp_path / "data" / "dev"
        data_root.mkdir(parents=True)
        (data_root / "last_update_status.json").write_text(
            json.dumps(
                {
                    "environment": "development",
                    "status": "success",
                    "api_key": "sk-secret-value",
                }
            ),
            encoding="utf-8",
        )
        loaded = load_last_update_status(data_root)
        assert loaded is not None
        assert loaded["status"] == "success"
        assert loaded.get("api_key") == "[REDACTED]"
