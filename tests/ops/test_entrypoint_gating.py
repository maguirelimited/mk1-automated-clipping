"""Entrypoint gating: Ops UI Run test + direct /jobs routes."""

from __future__ import annotations

import json
import os
import queue
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def va_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Video-automation test client with ungated jobs disabled."""
    # Ops UI create_app may have setdefault()'d MK04_RUNTIME_ROOT in this process.
    monkeypatch.delenv("MK04_REQUIRE_RUNTIME_PATHS", raising=False)
    monkeypatch.delenv("MK04_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("MK04_ENV", "dev")
    monkeypatch.setenv("MK04_ALLOW_UNGATED_JOBS", "0")
    monkeypatch.delenv("VIDEO_AUTOMATION_SECRET", raising=False)

    cfg_path = tmp_path / "pipeline_config.json"
    paths = {
        "input_folder": str(tmp_path / "input"),
        "output_folder": str(tmp_path / "output"),
        "temp_folder": str(tmp_path / "temp"),
        "jobs_folder": str(tmp_path / "jobs"),
        "analytics_folder": str(tmp_path / "analytics"),
    }
    cfg_path.write_text(
        json.dumps(
            {
                "paths": paths,
                "selection": {},
                "models": {},
                "chunking": {},
                "async_worker": {
                    "enabled": True,
                    "max_concurrent_jobs": 1,
                    "job_store_type": "json",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PIPELINE_CONFIG_PATH", str(cfg_path))
    for folder in paths.values():
        Path(folder).mkdir(parents=True, exist_ok=True)

    va_server = REPO_ROOT / "video-automation" / "server"
    if str(va_server) not in sys.path:
        sys.path.insert(0, str(va_server))
    # Prefer a clean import if a prior test imported a broken/partial app.
    if "app" in sys.modules and not hasattr(sys.modules["app"], "_create_job_from_payload"):
        sys.modules.pop("app", None)
    import app as server_app  # noqa: PLC0415

    server_app._JOB_WORKERS_STARTED = False
    server_app._JOB_RECOVERY_DONE = False
    while True:
        try:
            server_app._JOB_QUEUE.get_nowait()
            server_app._JOB_QUEUE.task_done()
        except queue.Empty:
            break

    with server_app.app.test_client() as client:
        yield client


def test_ops_ui_run_funnel_routes_through_run_pipeline(tmp_path: Path) -> None:
    ops_ui = REPO_ROOT / "ops-ui"
    if str(ops_ui) not in sys.path:
        sys.path.insert(0, str(ops_ui))
    from ops_ui.app import create_app
    from ops_ui.config import ServiceConfig, Settings
    from ops_ui.controls import ActionResult

    settings = Settings(
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
    app = create_app(settings)
    client = app.test_client()
    with mock.patch("ops_ui.app.execute_control_action") as exec_action:
        exec_action.return_value = ActionResult(True, "run_pipeline_dev", "pipeline ok")
        resp = client.post(
            "/funnels/run",
            data={"funnel_id": "demo_funnel", "next": "funnels"},
        )
        assert resp.status_code == 302
        exec_action.assert_called_once()
        _settings_arg, action = exec_action.call_args.args[:2]
        assert action == "run_pipeline_dev"
        assert exec_action.call_args.kwargs.get("funnel_id") == "demo_funnel"
    # Avoid poisoning later VA imports in the same pytest process.
    os.environ.pop("MK04_RUNTIME_ROOT", None)


def test_direct_jobs_rejects_ungated(va_client) -> None:
    resp = va_client.post("/jobs", json={"video_path": "/tmp/missing.mp4"})
    assert resp.status_code == 403
    data = resp.get_json() or {}
    assert "orchestration" in str(data.get("error") or "").lower()


def test_direct_process_rejects_ungated(va_client) -> None:
    resp = va_client.post("/process", json={"video_path": "/tmp/missing.mp4"})
    assert resp.status_code == 403
