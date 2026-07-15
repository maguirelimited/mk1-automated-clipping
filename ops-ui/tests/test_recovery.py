from __future__ import annotations

import json
from pathlib import Path

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.control_export import export_control_flags, read_controls_file
from ops_ui.recovery import (
    build_upload_failure,
    can_retry_upload,
    collect_failed_jobs,
    detect_stuck_video_jobs,
    is_dead_letter_upload,
)
from ops_ui.store import ControlStore


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
        stuck_running_sec=100.0,
        stuck_queued_sec=50.0,
        stuck_uploading_sec=50.0,
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


def test_export_controls_writes_json(tmp_path: Path) -> None:
    store = ControlStore(tmp_path / "ops.sqlite3", controls_file=tmp_path / "controls.json")
    store.init_db()
    store.set_control_bool("ingestion_paused", True)  # syncs controls.json
    data = read_controls_file(tmp_path / "controls.json")
    assert data["ingestion_paused"] is True
    assert data["uploads_paused"] is False


def test_failed_jobs_page_renders(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = ControlStore(settings.control_db_path, controls_file=settings.controls_file)
    store.init_db()
    app = create_app(settings)
    response = app.test_client().get("/failed")
    assert response.status_code == 200
    assert b"Legacy failed jobs" in response.data
    assert b"Dead-letter queue" in response.data


def test_recovery_page_renders(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    response = app.test_client().get("/recovery")
    assert response.status_code == 200
    assert b"Reliability safeguards" in response.data


def test_collect_failed_jobs_merges_sources() -> None:
    settings = _settings(Path("/tmp"))
    rows = collect_failed_jobs(
        [{"job_id": "job_1", "status": "failed", "current_stage": "transcribe", "error_count": 1}],
        [{"id": 9, "status": "failed_retryable", "last_error": "quota", "attempt_count": 2}],
        settings=settings,
        enrich_video=False,
    )
    assert len(rows) == 2
    assert rows[0]["service"] in {"video-automation", "output-funnel"}


def test_stuck_detection_flags_old_running_job() -> None:
    stuck = detect_stuck_video_jobs(
        [
            {
                "job_id": "job_x",
                "status": "running",
                "current_stage": "transcribe",
                "started_at": "2026-01-01T00:00:00Z",
                "heartbeat_at": "2020-01-01T00:00:00Z",
            }
        ],
        running_threshold_sec=60,
        queued_threshold_sec=60,
    )
    assert len(stuck) == 1
    assert stuck[0]["id"] == "job_x"
    assert stuck[0]["since"] == "2020-01-01T00:00:00Z"
    assert stuck[0]["can_cancel"] is True
    assert "possibly stuck" in stuck[0]["detail"]


def test_dead_letter_and_retryable_upload() -> None:
    terminal = {"id": 1, "status": "failed_terminal", "last_error": "max attempts"}
    assert is_dead_letter_upload(terminal)
    row = build_upload_failure(terminal)
    assert row["is_dead_letter"] is True
    assert can_retry_upload(terminal)
