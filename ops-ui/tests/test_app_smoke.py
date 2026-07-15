from __future__ import annotations

from pathlib import Path

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


def test_dashboard_renders_when_services_are_offline(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    response = app.test_client().get("/dashboard")

    assert response.status_code == 200
    assert b"Mission Control" in response.data
    assert b"source-input" in response.data

    publishing = app.test_client().get("/publishing")
    assert publishing.status_code == 200
    assert b"Upload Queue" in publishing.data

    clip_review = app.test_client().get("/clip-review")
    assert clip_review.status_code in {302, 301}
    assert "/ops/outputs" in (clip_review.headers.get("Location") or "")
