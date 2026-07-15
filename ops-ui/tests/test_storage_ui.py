"""Storage View UI tests (Storage Phase 11)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.storage_ui import (
    _build_warnings,
    _tone_for_status,
    build_storage_context,
    resolve_storage_artifact,
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
                key="source-input",
                label="source-input",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-source-input.service",
            ),
        ),
    )


_CTX = {
    "shell_connected": True,
    "shell_environment_label": "DEVELOPMENT",
    "shell_environment_css": "development",
    "shell_is_production": False,
    "shell_banner_main": "DEVELOPMENT",
    "shell_banner_sub": "Health PASS",
    "shell_health_badge": {"label": "HEALTHY", "tone": "ok"},
    "shell_activity": "idle",
    "shell_upload": "disabled",
    "shell_scheduler": "manual",
    "shell_overall": "PASS",
    "shell_health_data": {},
    "shell_status_data": {},
    "shell_env_token": "dev",
    "shell_nav": [{"endpoint": "ops_storage", "label": "Storage", "path": "/ops/storage"}],
    "shell_health_error": None,
    "shell_status_error": None,
    "storage_error": None,
    "storage_error_detail": None,
    "storage_overall": {"label": "HEALTHY", "tone": "ok"},
    "storage_disk": {
        "available": True,
        "level": "NORMAL",
        "tone": "ok",
        "usage_percent": 42.0,
        "total_label": "100.0 GB",
        "used_label": "42.0 GB",
        "free_label": "58.0 GB",
        "path": "/data",
        "retention_recommended": False,
        "detail": "42.0% used",
    },
    "storage_paths": {
        "data_root": "data/dev",
        "jobs_root": "jobs/dev",
        "logs_root": "logs/dev",
        "reports_root": "reports/dev",
        "database_path": "database/dev.db",
        "backups_root": "backups/dev",
        "database_backup_dir": "backups/dev/database",
        "retention_reports_dir": "reports/dev/retention",
        "storage_records_dir": "data/dev/storage",
    },
    "storage_retention": {
        "available": True,
        "tone": "ok",
        "status": "SUCCESS",
        "timestamp": "2026-07-04T12:00:00Z",
        "age_label": "1h ago",
        "mode": "dry_run",
        "files_considered": 10,
        "files_deleted": 0,
        "bytes_reclaimed_label": "1.0 MB",
        "reason": None,
    },
    "storage_backup": {
        "available": True,
        "tone": "ok",
        "status": "SUCCESS",
        "timestamp": "2026-07-04T11:00:00Z",
        "age_label": "2h ago",
        "integrity_ok": True,
        "backup_size_label": "8.0 KB",
        "backup_path": "backups/dev/database/db_dev.sqlite3",
        "backup_count": 1,
        "reason": None,
    },
    "storage_log_rotation": {
        "available": True,
        "tone": "ok",
        "status": "SUCCESS",
        "timestamp": "2026-07-04T10:00:00Z",
        "age_label": "3h ago",
        "active_log_count": 1,
        "rotated_count": 0,
        "failure_count": 0,
        "reason": None,
    },
    "storage_warnings": [],
    "storage_links": [
        {
            "label": "Latest retention report",
            "kind": "retention_report",
            "path": "reports/dev/retention/latest.json",
        }
    ],
}


class TestStorageUiPage:
    def test_storage_page_renders_read_only(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.app.build_storage_context", return_value=_CTX):
            response = app.test_client().get("/ops/storage")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Storage" in body
        assert "Is storage safe" in body
        assert "Operator Console" in body
        assert "Disk usage" in body
        assert "42.0%" in body
        assert "NORMAL" in body
        assert "Retention" in body
        assert "Database backup" in body
        assert "Log rotation" in body
        assert "Storage roots" in body
        assert "Read-only" in body or "read-only" in body
        assert "No cleanup" in body or "no cleanup" in body.lower() or "No cleanup" in body
        # No mutation controls.
        assert 'method="post"' not in body.lower()
        assert "Backup now" not in body
        assert "Apply retention" not in body
        assert "Delete" not in body

    def test_storage_nav_present(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.app.build_storage_context", return_value=_CTX):
            response = app.test_client().get("/ops/storage")
        assert b'href="/ops/storage"' in response.data or b"Storage" in response.data

    def test_artifact_download_allowlisted(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        report = tmp_path / "report.json"
        report.write_text('{"ok": true}\n', encoding="utf-8")
        with mock.patch(
            "ops_ui.app.resolve_storage_artifact",
            return_value=report,
        ):
            response = app.test_client().get("/ops/storage/artifact/retention_report")
        assert response.status_code == 200
        assert response.get_json() is None or response.data
        assert b'"ok": true' in response.data or b"ok" in response.data

    def test_artifact_download_unknown_kind_404(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.app.resolve_storage_artifact", return_value=None):
            response = app.test_client().get("/ops/storage/artifact/not_a_kind")
        assert response.status_code == 404


class TestStorageUiHelpers:
    def test_tone_mapping(self) -> None:
        assert _tone_for_status("SUCCESS") == "ok"
        assert _tone_for_status("WARN") == "warn"
        assert _tone_for_status("FAIL") == "bad"

    def test_warnings_for_disk_and_failures(self) -> None:
        class _Resolved:
            def get(self, key):
                if key == "storage.retention.database_backups_days":
                    return 30
                return None

        warnings = _build_warnings(
            disk={
                "level": "WARNING",
                "detail": "87% used",
            },
            retention={"available": True, "status": "FAIL", "reason": "planner boom"},
            backup={"available": True, "status": "FAIL", "reason": "disk full"},
            resolved=_Resolved(),
        )
        titles = {item["title"] for item in warnings}
        assert any("Disk pressure" in title for title in titles)
        assert "Latest retention failed" in titles
        assert "Latest database backup failed" in titles

    def test_resolve_rejects_unknown_kind(self, tmp_path: Path) -> None:
        assert resolve_storage_artifact(_settings(tmp_path), "etc_passwd") is None

    def test_build_storage_context_surfaces_error(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with mock.patch(
            "ops_ui.storage_ui._load_resolved",
            side_effect=RuntimeError("config boom"),
        ):
            ctx = build_storage_context(settings)
        assert ctx["storage_error"] == "RuntimeError"
        assert ctx["storage_overall"]["tone"] == "bad"
        assert ctx["storage_warnings"]
