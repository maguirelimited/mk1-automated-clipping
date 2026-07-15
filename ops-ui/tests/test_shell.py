"""Operations UI shell tests (Phase 6)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.shell import SHELL_NAV_LEGACY, SHELL_NAV_PRIMARY, SHELL_NAV_SECONDARY, build_shell_context


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


class TestShellContext:
    def test_dev_and_prod_labels(self, tmp_path: Path) -> None:
        dev = build_shell_context(_settings(tmp_path, environment="dev"))
        prod = build_shell_context(_settings(tmp_path, environment="prod"))
        assert dev["shell_environment_label"] == "DEVELOPMENT"
        assert dev["shell_environment_css"] == "development"
        assert dev["shell_is_production"] is False
        assert prod["shell_environment_label"] == "PRODUCTION"
        assert prod["shell_environment_css"] == "production"
        assert prod["shell_is_production"] is True

    def test_disconnected_backend_does_not_fabricate_health(
        self, tmp_path: Path
    ) -> None:
        with mock.patch("ops_ui.shell.build_system_health", side_effect=RuntimeError("down")):
            with mock.patch(
                "ops_ui.shell.build_system_status", side_effect=RuntimeError("down")
            ):
                ctx = build_shell_context(_settings(tmp_path))
        assert ctx["shell_connected"] is False
        assert ctx["shell_health_badge"]["label"] == "DISCONNECTED"
        assert ctx["shell_health_badge"]["tone"] == "bad"
        assert ctx["shell_activity"] == "unknown"
        assert ctx["shell_upload"] == "unknown"
        assert ctx["shell_scheduler"] == "unknown"
        assert "DISCONNECTED" in ctx["shell_banner_main"]

    def test_nav_primary_and_secondary_structure(self, tmp_path: Path) -> None:
        ctx = build_shell_context(_settings(tmp_path))
        primary_labels = [item["label"] for item in ctx["shell_nav_primary"]]
        secondary_labels = [item["label"] for item in ctx["shell_nav_secondary"]]
        legacy_labels = [item["label"] for item in ctx["shell_nav_legacy"]]
        assert primary_labels == [label for _, label, _ in SHELL_NAV_PRIMARY]
        assert secondary_labels == [label for _, label, _ in SHELL_NAV_SECONDARY]
        assert legacy_labels == [label for _, label, _ in SHELL_NAV_LEGACY]
        assert ctx["shell_nav_primary"][0]["label"] == "Console"
        assert ctx["shell_nav_primary"][0]["path"] == "/ops"
        assert "Health" in secondary_labels
        assert "Logs" in secondary_labels
        assert "Health" not in legacy_labels
        assert "Logs" not in legacy_labels


class TestShellRendering:
    def test_environment_banner_and_status_header(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path, environment="dev"))
        response = app.test_client().get("/ops")
        assert response.status_code == 200
        body = response.data
        assert b"DEVELOPMENT" in body
        assert b"env-development" in body
        assert b"status-header" in body
        assert b"Health" in body
        assert b"Activity" in body
        assert b"Upload" in body
        assert b"Scheduler" in body

    def test_production_banner_distinct_from_dev(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path, environment="prod"))
        response = app.test_client().get("/ops")
        body = response.data
        assert b"PRODUCTION" in body
        assert b"env-production" in body
        assert b"DEVELOPMENT" not in body

    def test_primary_navigation_order(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        client = app.test_client()
        overview = client.get("/ops")
        assert overview.status_code == 200
        body = overview.data
        assert b"Console" in body
        assert b"Overview" not in body
        assert b"Outputs" in body
        assert b"Runs" in body
        assert b"Jobs" in body
        assert b"Failures" in body
        assert b"Storage" in body
        assert b"Configuration" in body

        console_pos = body.index(b'href="/ops"')
        outputs_pos = body.index(b'href="/ops/outputs"')
        runs_pos = body.index(b'href="/ops/runs"')
        jobs_pos = body.index(b'href="/ops/jobs"')
        failures_pos = body.index(b'href="/ops/failures"')
        storage_pos = body.index(b'href="/ops/storage"')
        health_pos = body.index(b'href="/health"')
        assert console_pos < outputs_pos < runs_pos < jobs_pos < failures_pos < storage_pos
        assert health_pos > failures_pos

        assert client.get("/ops/runs").status_code == 200
        assert client.get("/ops/jobs").status_code == 200
        assert client.get("/ops/outputs").status_code == 200
        assert client.get("/ops/failures").status_code == 200
        config_page = client.get("/ops/configuration")
        assert config_page.status_code == 200
        assert b"Configuration" in config_page.data
        assert b"Is configuration valid" in config_page.data
        assert b"navigation placeholder" not in config_page.data.lower()

    def test_secondary_and_legacy_navigation(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        body = app.test_client().get("/ops").data
        assert b"Advanced / Legacy" in body
        assert b"Mission Control" in body
        assert b"Legacy clip review" not in body
        assert b"Legacy failed jobs" in body
        assert b"Legacy settings" in body
        assert b"Daily loop" in body
        assert b"Diagnostics" in body
        assert b'href="/health"' in body
        assert b'href="/logs"' in body
        # Legacy bar should not duplicate diagnostic Health/Logs links.
        legacy_nav = body.split(b'class="legacy-nav"', 1)[1].split(b"</nav>", 1)[0]
        assert b'href="/health"' not in legacy_nav
        assert b'href="/logs"' not in legacy_nav

    def test_legacy_tools_not_in_primary_nav(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        body = app.test_client().get("/ops").data
        primary_section = body.split(b'ops-nav-primary', 1)[1].split(b'ops-nav-secondary', 1)[0]
        assert b"Mission Control" not in primary_section
        assert b"Publishing" not in primary_section
        assert b"Legacy clip review" not in primary_section

    def test_active_nav_on_outputs_page(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch(
            "ops_ui.outputs_ui.latest_run_id_with_clips",
            return_value=None,
        ):
            body = app.test_client().get("/ops/outputs").data
        assert b'href="/ops/outputs"' in body
        assert b'class="active"' in body
        assert b"Outputs" in body

    def test_root_redirects_to_ops_overview(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        response = app.test_client().get("/")
        assert response.status_code in {302, 301}
        assert "/ops" in (response.headers.get("Location") or "")

    def test_disconnected_state_renders_safely(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.shell.build_system_health", side_effect=RuntimeError("x")):
            with mock.patch("ops_ui.shell.build_system_status", side_effect=RuntimeError("x")):
                response = app.test_client().get("/ops")
        assert response.status_code == 200
        assert b"DISCONNECTED" in response.data
        assert b"not fabricated" in response.data.lower() or b"DISCONNECTED" in response.data

    def test_safe_controls_use_csrf_forms(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        response = app.test_client().get("/ops")
        body = response.data
        assert b"Safe actions" in body
        assert b'name="csrf_token"' in body
        assert b"/ops/actions/" in body
        # No arbitrary command box / terminal.
        assert b"<textarea" not in body.lower()
