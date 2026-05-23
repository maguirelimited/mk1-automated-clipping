from __future__ import annotations

import json
from pathlib import Path

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.funnels import build_funnel_rows, scan_input_ledger
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
        stuck_running_sec=7200.0,
        stuck_queued_sec=1800.0,
        stuck_uploading_sec=1800.0,
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


def test_funnels_page_renders(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    response = app.test_client().get("/funnels")
    assert response.status_code == 200
    assert b"Funnel Management" not in response.data  # eyebrow is lowercase
    assert b"Funnels" in response.data
    assert b"Feature coverage" in response.data


def test_build_funnel_rows_aggregates_ledger_and_jobs(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    path = ledger / "input_20260523T100000Z_demo.json"
    path.write_text(
        json.dumps(
            {
                "input_id": "input_20260523T100000Z_demo",
                "funnel_id": "demo_funnel",
                "created_at": "2026-05-23T10:00:00+00:00",
                "state": "input_ready",
                "title": "Episode 1",
            }
        ),
        encoding="utf-8",
    )

    store = ControlStore(tmp_path / "ops.sqlite3")
    store.init_db()

    input_map, runs = scan_input_ledger(ledger)
    rows = build_funnel_rows(
        settings=_settings(tmp_path),
        store=store,
        source_funnels=[
            {
                "funnel_id": "demo_funnel",
                "active": True,
                "angle": "test",
                "pipeline_profile": "demo_funnel",
                "source_type": "youtube_channels",
                "sources": [{"label": "Chan", "url": "https://example.com", "active": True}],
                "posting_config": {"platforms": ["youtube_shorts"]},
            }
        ],
        video_jobs=[
            {
                "job_id": "job_1",
                "input_id": "input_20260523T100000Z_demo",
                "status": "running",
            }
        ],
        upload_jobs=[
            {"id": 1, "funnel_id": "demo_funnel", "status": "planned"},
            {"id": 2, "funnel_id": "demo_funnel", "status": "failed_upload"},
        ],
        ingestion_paused=False,
        input_funnel_map=input_map,
        input_runs=runs,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["funnel_id"] == "demo_funnel"
    assert row["active_video_jobs"] == 1
    assert row["queue_depth"] == 1
    assert row["failure_count"] == 1
    assert row["last_success_at"] == "2026-05-23T10:00:00+00:00"
    assert len(row["active_sources"]) == 1


def test_pause_funnel_blocks_run(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = app.test_client()
    pause = client.post("/funnels/demo_funnel/pause")
    assert pause.status_code == 302
    run = client.post("/funnels/run", data={"funnel_id": "demo_funnel", "next": "funnels"})
    assert run.status_code == 302
    with client.session_transaction() as sess:
        flashes = sess.get("_flashes", [])
    assert any("paused" in str(msg).lower() for _cat, msg in flashes)
