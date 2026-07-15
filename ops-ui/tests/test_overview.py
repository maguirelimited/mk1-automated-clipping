"""Operator Console page tests."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.overview import build_overview_context
from ops_ui.shell import build_shell_context


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


def _health_payload(**overrides):
    data = {
        "overall": "WARN",
        "environment": "dev",
        "boot_readiness": "READY",
        "disk": {"status": "PASS", "usage_percent": 42.0, "detail": "ok"},
        "upload": {"enabled": False, "status": "pass", "detail": "disabled by config"},
        "scheduler": {"effective": "manual", "status": "pass", "detail": "manual mode"},
        "services": [],
        "readiness_failures": [],
        "execution_lock": {"present": False, "stale": False},
    }
    data.update(overrides)
    return data


def _status_payload(**overrides):
    data = {
        "environment": "dev",
        "state": "idle",
        "active_run": None,
        "current_activity": "Nothing in progress.",
        "queue": {"pending": 0, "running": 0, "failed": 0},
        "recent_summary": {"runs": 0},
    }
    data.update(overrides)
    return data


class TestOverviewContext:
    def test_uses_shared_shell_health_and_status(self, tmp_path: Path) -> None:
        shell = {
            "shell_connected": True,
            "shell_is_production": False,
            "shell_env_token": "dev",
            "shell_environment_label": "DEVELOPMENT",
            "shell_overall": "PASS",
            "shell_activity": "idle",
            "shell_upload": "disabled",
            "shell_scheduler": "manual",
            "shell_health_data": _health_payload(overall="PASS"),
            "shell_status_data": _status_payload(),
        }
        with mock.patch(
            "ops_ui.overview.services_payload",
            return_value={
                "services": [
                    {
                        "service_name": "api",
                        "health": "PASS",
                        "state": "active",
                        "detail": "ok",
                    },
                    {
                        "service_name": "worker",
                        "health": "FAIL",
                        "state": "failed",
                        "detail": "down",
                    },
                ]
            },
        ):
            with mock.patch(
                "ops_ui.overview.runs_list_payload",
                return_value={
                    "runs": [
                        {
                            "run_id": "run_1",
                            "trigger": "manual_cli",
                            "status": "SUCCESS",
                            "started_at": "2026-07-04T00:00:00Z",
                            "finished_at": "2026-07-04T00:01:00Z",
                            "duration_seconds": 60,
                            "jobs_completed": 1,
                            "jobs_failed": 0,
                        }
                    ]
                },
            ):
                ctx = build_overview_context(_settings(tmp_path), shell=shell)

        assert ctx["overview_overall"] == "PASS"
        assert ctx["console_health_headline"] == "Healthy"
        assert ctx["overview_activity"] == "idle"
        assert ctx["overview_upload"] == "disabled"
        assert ctx["console_service_summary"]["text"] == "1 service unhealthy: Worker"
        assert ctx["console_run_cards"][0]["run_id"] == "run_1"
        assert ctx["console_run_cards"][0]["outputs_href"] == "/ops/outputs?run_id=run_1"
        assert any(
            item["label"] == "Worker unhealthy" for item in ctx["console_attention"]
        )
        assert any(
            item["label"] == "Uploads disabled" for item in ctx["console_attention"]
        )

    def test_disconnected_does_not_fabricate_values(self, tmp_path: Path) -> None:
        with mock.patch("ops_ui.shell.build_system_health", side_effect=RuntimeError("x")):
            with mock.patch("ops_ui.shell.build_system_status", side_effect=RuntimeError("x")):
                shell = build_shell_context(_settings(tmp_path))
        ctx = build_overview_context(_settings(tmp_path), shell=shell)
        assert ctx["overview_connected"] is False
        assert ctx["console_health_headline"] == "Unknown / Disconnected"
        assert ctx["console_actions_disabled"] is True
        assert ctx["overview_services"] == []
        assert ctx["console_run_cards"] == []
        assert ctx["console_attention"][0]["label"] == "Observability backend disconnected"

    def test_empty_runs_state(self, tmp_path: Path) -> None:
        shell = {
            "shell_connected": True,
            "shell_is_production": False,
            "shell_env_token": "dev",
            "shell_environment_label": "DEVELOPMENT",
            "shell_overall": "PASS",
            "shell_activity": "idle",
            "shell_upload": "enabled",
            "shell_scheduler": "enabled",
            "shell_health_data": _health_payload(
                overall="PASS",
                upload={"enabled": True, "status": "pass"},
                scheduler={"effective": "enabled", "status": "pass"},
            ),
            "shell_status_data": _status_payload(),
        }
        with mock.patch("ops_ui.overview.services_payload", return_value={"services": []}):
            with mock.patch(
                "ops_ui.overview.runs_list_payload", return_value={"runs": []}
            ):
                ctx = build_overview_context(_settings(tmp_path), shell=shell)
        assert ctx["console_runs_empty"] is True
        assert ctx["console_service_summary"]["text"] == "All core services healthy."
        assert ctx["overview_attention_empty"] is True

    def test_active_and_last_completed_run_cards(self, tmp_path: Path) -> None:
        shell = {
            "shell_connected": True,
            "shell_is_production": False,
            "shell_env_token": "dev",
            "shell_environment_label": "DEVELOPMENT",
            "shell_overall": "PASS",
            "shell_activity": "running",
            "shell_upload": "enabled",
            "shell_scheduler": "enabled",
            "shell_health_data": _health_payload(overall="PASS"),
            "shell_status_data": _status_payload(
                state="running",
                active_run={
                    "run_id": "run_active",
                    "status": "RUNNING",
                    "trigger": "operations_ui",
                    "started_at": "2026-07-04T01:00:00Z",
                },
            ),
        }
        with mock.patch(
            "ops_ui.overview.services_payload", return_value={"services": []}
        ):
            with mock.patch(
                "ops_ui.overview.runs_list_payload",
                return_value={
                    "runs": [
                        {
                            "run_id": "run_active",
                            "status": "RUNNING",
                            "started_at": "2026-07-04T01:00:00Z",
                        },
                        {
                            "run_id": "run_prev",
                            "status": "SUCCESS",
                            "finished_at": "2026-07-04T00:30:00Z",
                            "duration_seconds": 120,
                            "jobs_completed": 2,
                        },
                    ]
                },
            ):
                ctx = build_overview_context(_settings(tmp_path), shell=shell)

        titles = [c["title"] for c in ctx["console_run_cards"]]
        assert titles == ["Active run", "Last completed run"]
        assert ctx["console_run_cards"][1]["run_id"] == "run_prev"
        active_links = [l["label"] for l in ctx["console_run_cards"][0]["links"]]
        assert "Monitor run" in active_links

    def test_failed_run_card_links(self, tmp_path: Path) -> None:
        shell = {
            "shell_connected": True,
            "shell_is_production": False,
            "shell_env_token": "dev",
            "shell_environment_label": "DEVELOPMENT",
            "shell_overall": "PASS",
            "shell_activity": "idle",
            "shell_upload": "enabled",
            "shell_scheduler": "enabled",
            "shell_health_data": _health_payload(overall="PASS"),
            "shell_status_data": _status_payload(),
        }
        with mock.patch(
            "ops_ui.overview.services_payload", return_value={"services": []}
        ):
            with mock.patch(
                "ops_ui.overview.runs_list_payload",
                return_value={
                    "runs": [
                        {
                            "run_id": "run_bad",
                            "status": "FAIL",
                            "failure_summary": {"reason": "worker down"},
                            "jobs_failed": 1,
                        }
                    ]
                },
            ):
                ctx = build_overview_context(_settings(tmp_path), shell=shell)
        card = ctx["console_run_cards"][0]
        labels = [link["label"] for link in card["links"]]
        assert "Inspect failures" in labels
        assert any("/ops/runs/run_bad" in link["href"] for link in card["links"])


class TestConsoleAttention:
    def test_attention_empty_when_healthy(self, tmp_path: Path) -> None:
        shell = {
            "shell_connected": True,
            "shell_is_production": False,
            "shell_env_token": "dev",
            "shell_environment_label": "DEVELOPMENT",
            "shell_overall": "PASS",
            "shell_activity": "idle",
            "shell_upload": "enabled",
            "shell_scheduler": "enabled",
            "shell_health_data": _health_payload(overall="PASS"),
            "shell_status_data": _status_payload(),
        }
        with mock.patch("ops_ui.overview.services_payload", return_value={"services": []}):
            with mock.patch(
                "ops_ui.overview.runs_list_payload", return_value={"runs": []}
            ):
                ctx = build_overview_context(_settings(tmp_path), shell=shell)
        assert ctx["overview_attention_empty"] is True
        assert ctx["console_attention"] == []

    def test_last_run_failed_attention_item(self, tmp_path: Path) -> None:
        shell = {
            "shell_connected": True,
            "shell_is_production": False,
            "shell_env_token": "dev",
            "shell_environment_label": "DEVELOPMENT",
            "shell_overall": "PASS",
            "shell_activity": "idle",
            "shell_upload": "enabled",
            "shell_scheduler": "enabled",
            "shell_health_data": _health_payload(overall="PASS"),
            "shell_status_data": _status_payload(),
        }
        with mock.patch("ops_ui.overview.services_payload", return_value={"services": []}):
            with mock.patch(
                "ops_ui.overview.runs_list_payload",
                return_value={
                    "runs": [
                        {
                            "run_id": "run_bad",
                            "status": "FAIL",
                            "failure_summary": {"reason": "worker down"},
                        }
                    ]
                },
            ):
                ctx = build_overview_context(_settings(tmp_path), shell=shell)
        item = next(i for i in ctx["console_attention"] if i["title"] == "Last run failed")
        assert item["severity"] == "action"
        assert item["href"] == "/ops/runs/run_bad"
        assert "inspect the failed stage" in item["explanation"].lower()
        assert item["detail"] == "worker down"

    def test_service_unhealthy_attention_item(self, tmp_path: Path) -> None:
        shell = {
            "shell_connected": True,
            "shell_is_production": False,
            "shell_env_token": "dev",
            "shell_environment_label": "DEVELOPMENT",
            "shell_overall": "PASS",
            "shell_activity": "idle",
            "shell_upload": "enabled",
            "shell_scheduler": "enabled",
            "shell_health_data": _health_payload(overall="PASS"),
            "shell_status_data": _status_payload(),
        }
        with mock.patch(
            "ops_ui.overview.services_payload",
            return_value={
                "services": [
                    {
                        "service_name": "scheduler",
                        "health": "FAIL",
                        "state": "stopped",
                        "detail": "unit inactive",
                    }
                ]
            },
        ):
            with mock.patch(
                "ops_ui.overview.runs_list_payload", return_value={"runs": []}
            ):
                ctx = build_overview_context(_settings(tmp_path), shell=shell)
        item = next(
            i for i in ctx["console_attention"] if i["title"] == "Scheduler unhealthy"
        )
        assert item["href"] == "/ops/failures"
        assert item["action_label"] == "Inspect failures"

    def test_scheduler_stopped_attention_item(self, tmp_path: Path) -> None:
        shell = {
            "shell_connected": True,
            "shell_is_production": False,
            "shell_env_token": "dev",
            "shell_environment_label": "DEVELOPMENT",
            "shell_overall": "PASS",
            "shell_activity": "idle",
            "shell_upload": "enabled",
            "shell_scheduler": "stopped",
            "shell_health_data": _health_payload(overall="PASS"),
            "shell_status_data": _status_payload(),
        }
        with mock.patch("ops_ui.overview.services_payload", return_value={"services": []}):
            with mock.patch(
                "ops_ui.overview.runs_list_payload", return_value={"runs": []}
            ):
                ctx = build_overview_context(_settings(tmp_path), shell=shell)
        item = next(
            i for i in ctx["console_attention"] if i["title"] == "Scheduler not running"
        )
        assert item["severity"] == "warning"
        assert item["href"] == "/ops/configuration"

    def test_disk_warning_attention_item(self, tmp_path: Path) -> None:
        shell = {
            "shell_connected": True,
            "shell_is_production": False,
            "shell_env_token": "dev",
            "shell_environment_label": "DEVELOPMENT",
            "shell_overall": "PASS",
            "shell_activity": "idle",
            "shell_upload": "enabled",
            "shell_scheduler": "enabled",
            "shell_health_data": _health_payload(
                overall="PASS",
                disk={"status": "WARN", "usage_percent": 91.0, "detail": "91% used"},
            ),
            "shell_status_data": _status_payload(),
        }
        with mock.patch("ops_ui.overview.services_payload", return_value={"services": []}):
            with mock.patch(
                "ops_ui.overview.runs_list_payload", return_value={"runs": []}
            ):
                ctx = build_overview_context(_settings(tmp_path), shell=shell)
        item = next(i for i in ctx["console_attention"] if i["title"] == "Disk space low")
        assert item["severity"] == "warning"
        assert item["href"] == "/ops/storage"

    def test_config_not_ready_attention_item(self, tmp_path: Path) -> None:
        shell = {
            "shell_connected": True,
            "shell_is_production": False,
            "shell_env_token": "dev",
            "shell_environment_label": "DEVELOPMENT",
            "shell_overall": "PASS",
            "shell_activity": "idle",
            "shell_upload": "enabled",
            "shell_scheduler": "enabled",
            "shell_health_data": _health_payload(
                overall="PASS", readiness_failures=["API unreachable"]
            ),
            "shell_status_data": _status_payload(),
        }
        with mock.patch("ops_ui.overview.services_payload", return_value={"services": []}):
            with mock.patch(
                "ops_ui.overview.runs_list_payload", return_value={"runs": []}
            ):
                ctx = build_overview_context(_settings(tmp_path), shell=shell)
        item = next(
            i
            for i in ctx["console_attention"]
            if i["title"] == "Configuration not ready: API unreachable"
        )
        assert item["href"] == "/ops/configuration"

    def test_disconnected_attention_item_has_no_fake_links(self, tmp_path: Path) -> None:
        with mock.patch("ops_ui.shell.build_system_health", side_effect=RuntimeError("x")):
            with mock.patch("ops_ui.shell.build_system_status", side_effect=RuntimeError("x")):
                shell = build_shell_context(_settings(tmp_path))
        ctx = build_overview_context(_settings(tmp_path), shell=shell)
        item = ctx["console_attention"][0]
        assert item["title"] == "Observability backend disconnected"
        assert item["href"] is None
        assert "not fabricated" in item["explanation"].lower()

    def test_no_attention_when_data_missing_for_optional_checks(self, tmp_path: Path) -> None:
        shell = {
            "shell_connected": True,
            "shell_is_production": False,
            "shell_env_token": "dev",
            "shell_environment_label": "DEVELOPMENT",
            "shell_overall": "PASS",
            "shell_activity": "idle",
            "shell_upload": "enabled",
            "shell_scheduler": "enabled",
            "shell_health_data": _health_payload(overall="PASS"),
            "shell_status_data": _status_payload(),
        }
        with mock.patch("ops_ui.overview.services_payload", return_value={"services": []}):
            with mock.patch(
                "ops_ui.overview.runs_list_payload", return_value={"runs": []}
            ):
                ctx = build_overview_context(_settings(tmp_path), shell=shell)
        titles = {item["title"] for item in ctx["console_attention"]}
        assert "Last run failed" not in titles
        assert "Disk space low" not in titles
        assert "Active run failed" not in titles


class TestOverviewRendering:
    def test_console_renders_core_sections(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        response = app.test_client().get("/ops")
        assert response.status_code == 200
        body = response.data
        assert b"Operator Console" in body
        assert b"Overall health" in body
        assert b"Environment &amp; safety" in body or b"Environment & safety" in body
        assert b"Needs attention" in body
        assert b"Current activity" in body
        assert b"Last run" in body
        assert b"Automation" in body
        assert b"Safe actions" in body
        assert b"Daily loop" in body
        assert b"Advanced / Legacy" in body
        assert b"Legacy clip review" not in body
        assert b"Deep tools outside the daily" in body
        assert b"Outputs" in body
        assert b"Recent runs" not in body
        assert b"Resources" not in body
        assert b"View all runs" not in body

    def test_pass_warn_fail_display(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        health = mock.Mock()
        health.to_dict.return_value = _health_payload(overall="FAIL", readiness_failures=["API"])
        status = mock.Mock()
        status.to_dict.return_value = _status_payload(state="blocked")
        with mock.patch("ops_ui.shell.build_system_health", return_value=health):
            with mock.patch("ops_ui.shell.build_system_status", return_value=status):
                with mock.patch(
                    "ops_ui.overview.services_payload",
                    return_value={
                        "services": [
                            {
                                "service_name": "api",
                                "health": "FAIL",
                                "state": "failed",
                                "detail": "down",
                            }
                        ]
                    },
                ):
                    with mock.patch(
                        "ops_ui.overview.runs_list_payload",
                        return_value={"runs": []},
                    ):
                        response = app.test_client().get("/ops")
        assert response.status_code == 200
        assert b"Action required" in response.data
        assert b"BLOCKED" in response.data
        assert b"System health failing" in response.data
        assert b"Configuration not ready: API" in response.data

    def test_disconnected_overview_renders(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.shell.build_system_health", side_effect=RuntimeError("x")):
            with mock.patch("ops_ui.shell.build_system_status", side_effect=RuntimeError("x")):
                response = app.test_client().get("/ops")
        assert response.status_code == 200
        assert b"DISCONNECTED" in response.data
        assert b"Unknown / Disconnected" in response.data
        assert b"not fabricated" in response.data.lower()
        assert b"Actions are unavailable" in response.data

    def test_safe_actions_present_with_csrf(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        body = app.test_client().get("/ops").data
        assert b"Safe actions" in body
        assert b"Run pipeline" in body
        assert b'name="funnel_id"' in body
        assert b'name="csrf_token"' in body
        assert b"/ops/actions/" in body
        assert b"run_pipeline_dev" in body
        assert b"restart_service" in body or b"Restart worker" in body

    def test_console_renders_funnel_configuration_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from test_funnels import _save_registry_funnel

        registry_dir = tmp_path / "registry"
        _save_registry_funnel(registry_dir)
        monkeypatch.setenv("OPS_FUNNEL_REGISTRY_DIR", str(registry_dir))

        app = create_app(_settings(tmp_path))
        body = app.test_client().get("/ops").data
        assert b"Funnel configuration" in body
        assert b"MFM Business AI" in body
        assert b"mfm_business_ai_001" in body
        assert b"Manage funnels" in body

    def test_attention_empty_state(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        health = mock.Mock()
        health.to_dict.return_value = _health_payload(
            overall="PASS",
            upload={"enabled": True, "status": "pass", "detail": "ok"},
            scheduler={"effective": "enabled", "status": "pass", "detail": "ok"},
        )
        status = mock.Mock()
        status.to_dict.return_value = _status_payload()
        with mock.patch("ops_ui.shell.build_system_health", return_value=health):
            with mock.patch("ops_ui.shell.build_system_status", return_value=status):
                with mock.patch(
                    "ops_ui.overview.services_payload", return_value={"services": []}
                ):
                    with mock.patch(
                        "ops_ui.overview.runs_list_payload", return_value={"runs": []}
                    ):
                        response = app.test_client().get("/ops")
        assert b"Nothing needs attention right now." in response.data
