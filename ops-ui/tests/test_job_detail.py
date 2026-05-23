from __future__ import annotations

from pathlib import Path

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
        stuck_running_sec=100.0,
        stuck_queued_sec=50.0,
        stuck_uploading_sec=50.0,
        services=(
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-video-automation.service",
            ),
        ),
    )


def test_video_job_detail_redirects_when_offline(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    response = app.test_client().get("/jobs/video/job_20260512T140000Z_abcdef12", follow_redirects=True)
    assert response.status_code == 200
    assert b"Failed Jobs" in response.data or b"not available" in response.data.lower()
