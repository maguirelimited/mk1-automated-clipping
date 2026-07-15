from __future__ import annotations

import json
import os
import queue
import time
import sys
from pathlib import Path

import pytest

SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "server"))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import app as server_app  # noqa: E402
from input_service import ledger as input_ledger  # noqa: E402
from input_service import paths as input_paths  # noqa: E402


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    server_app._JOB_WORKERS_STARTED = False
    server_app._JOB_RECOVERY_DONE = False
    while True:
        try:
            server_app._JOB_QUEUE.get_nowait()
            server_app._JOB_QUEUE.task_done()
        except queue.Empty:
            break
    cfg_path = tmp_path / "pipeline_config.json"
    paths = {
        "input_folder": str(tmp_path / "input"),
        "output_folder": str(tmp_path / "output"),
        "temp_folder": str(tmp_path / "temp"),
        "jobs_folder": str(tmp_path / "jobs"),
        "analytics_folder": str(tmp_path / "analytics"),
    }
    _write_json(
        cfg_path,
        {
            "paths": paths,
            "selection": {},
            "models": {},
            "chunking": {},
            "async_worker": {"enabled": True, "max_concurrent_jobs": 1, "job_store_type": "json"},
        },
    )
    monkeypatch.setenv("PIPELINE_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("MK04_ALLOW_UNGATED_JOBS", "1")
    monkeypatch.delenv("VIDEO_AUTOMATION_SECRET", raising=False)
    for folder in paths.values():
        Path(folder).mkdir(parents=True, exist_ok=True)
    with server_app.app.test_client() as c:
        yield c, Path(paths["jobs_folder"])


def _create_job(
    jobs_root: Path,
    *,
    job_id: str,
    created_at: str,
    status: str = "success",
    with_debug_artifacts: bool = False,
) -> dict:
    job_dir = jobs_root / f"source_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "review.md").write_text("# review\n", encoding="utf-8")
    clips_dir = job_dir / "clips"
    clips_dir.mkdir()
    (clips_dir / "clip_01.mp4").write_bytes(b"fake-video")

    report = {
        "job_id": job_id,
        "input_video_name": "source.mp4",
        "status": status,
        "created_at": created_at,
        "completed_at": created_at,
        "warnings": [{"category": "selection_validation"}],
        "errors": [] if status == "success" else [{"category": "selection_error"}],
        "stage_timings_ms": {"selection_ms": 123, "total_ms": 456},
        "policy_resolution": {"pipeline_profile": "business_podcasts_001"},
        "clips": [
            {
                "clip_id": f"{job_id}_clip_01",
                "clip_index": 1,
                "start": "00:00:01.000",
                "end": "00:00:31.000",
                "duration_sec": 30.0,
                "clip_file": "clip_01.mp4",
                "clip_path": "/tmp/output/clip_01.mp4",
                "title": "Useful Clip",
                "hook": "A good hook",
                "caption": "Short caption",
                "scores": {"hook_strength": 8},
                "clip_validation": {"ok": True, "ffprobe_duration_sec": 30.0},
            }
        ],
    }
    _write_json(job_dir / "report.json", report)

    if with_debug_artifacts:
        _write_json(
            job_dir / "transcript_payload.json",
            {
                "text": "x" * 5000,
                "language": "en",
                "duration": 45.0,
                "segments": [
                    {"start": 1.0, "end": 4.0, "text": "First line"},
                    {"start": 5.0, "end": 9.5, "text": "Second line"},
                ],
            },
        )
        _write_json(
            job_dir / "selection.json",
            {
                "clips": [
                    {
                        "start": "00:00:01.000",
                        "end": "00:00:31.000",
                        "duration_sec": 30.0,
                        "title": "Useful Clip",
                    }
                ],
                "validation_warnings": [{"message": "minor issue"}],
            },
        )
    return report


def test_list_jobs_sorts_newest_first_and_summarizes(client):
    c, jobs_root = client
    older = "job_20260511T120000Z_aaaaaaaa"
    newer = "job_20260512T120000Z_bbbbbbbb"
    _create_job(jobs_root, job_id=older, created_at="2026-05-11T12:00:00+00:00")
    _create_job(jobs_root, job_id=newer, created_at="2026-05-12T12:00:00+00:00")

    resp = c.get("/jobs?limit=1")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["count"] == 1
    assert data["jobs"][0]["job_id"] == newer
    assert data["jobs"][0]["clip_count"] == 1
    assert data["jobs"][0]["warning_count"] == 1
    assert data["jobs"][0]["error_count"] == 0
    assert data["jobs"][0]["artifacts"]["report_exists"] is True
    assert data["jobs"][0]["artifacts"]["review_exists"] is True
    assert data["jobs"][0]["job_url"] == f"/jobs/{newer}"
    assert data["jobs"][0]["debug_url"] == f"/jobs/{newer}/debug"


def test_doctor_reports_linux_readiness_fields(client):
    c, _ = client

    resp = c.get("/doctor")

    assert resp.status_code in (200, 500)
    checks = {check["name"] for check in resp.get_json()["checks"]}
    assert "python_executable" in checks
    assert "flask_import" in checks
    assert "path_writable:input" in checks


def test_secret_protects_non_health_endpoints(client, monkeypatch: pytest.MonkeyPatch):
    c, _ = client
    monkeypatch.setenv("VIDEO_AUTOMATION_SECRET", "secret-1")

    health = c.get("/healthz")
    assert health.status_code == 200

    denied = c.get("/jobs")
    assert denied.status_code == 401

    allowed = c.get("/jobs", headers={"X-Video-Automation-Secret": "secret-1"})
    assert allowed.status_code == 200


def test_get_job_returns_status_payload(client):
    c, jobs_root = client
    job_id = "job_20260512T130000Z_1234abcd"
    report = _create_job(jobs_root, job_id=job_id, created_at="2026-05-12T13:00:00+00:00")

    resp = c.get(f"/jobs/{job_id}")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["job_id"] == report["job_id"]
    assert data["status"] == "success"
    assert data["current_stage"] == "success"
    assert data["clips"][0]["title"] == "Useful Clip"
    assert data["artifacts"]["report"]["exists"] is True


def test_get_job_outputs_returns_clips_and_metadata(client):
    c, jobs_root = client
    job_id = "job_20260512T130000Z_1234abcd"
    _create_job(jobs_root, job_id=job_id, created_at="2026-05-12T13:00:00+00:00")

    resp = c.get(f"/jobs/{job_id}/outputs")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["ready"] is True
    assert data["clips"][0]["hook"] == "A good hook"
    assert data["metadata"]["clip_count"] == 1


def test_output_funnel_handoff_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    report_path = tmp_path / "report.json"
    report = {"job_id": "job_20260512T130000Z_1234abcd", "status": "success", "clips": []}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"success": true, "registered": []}'

    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        return FakeResponse()

    monkeypatch.setenv("OUTPUT_FUNNEL_URL", "http://output-funnel.test")
    monkeypatch.setattr(server_app.urlrequest, "urlopen", fake_urlopen)

    result = server_app._try_output_funnel_handoff(report, report_path=str(report_path))

    assert result["ok"] is True
    assert result["status_code"] == 200
    assert calls


def test_output_funnel_handoff_down_is_nonfatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    report_path = tmp_path / "report.json"
    report = {"job_id": "job_20260512T130000Z_1234abcd", "status": "success", "clips": []}

    def fake_urlopen(_req, timeout):
        raise OSError("connection refused")

    monkeypatch.setenv("OUTPUT_FUNNEL_URL", "http://127.0.0.1:5999")
    monkeypatch.setattr(server_app.urlrequest, "urlopen", fake_urlopen)

    result = server_app._try_output_funnel_handoff(report, report_path=str(report_path))

    assert result["enabled"] is True
    assert result["ok"] is False
    assert "connection refused" in result["error"]


def test_output_funnel_schedule_lines_include_publish_at():
    handoff = {
        "response": {
            "processing": {
                "schedule": {
                    "results": [
                        {
                            "upload_job_id": 7,
                            "scheduled": True,
                            "platform_publish_at": "2026-05-22T09:00:00Z",
                        },
                        {
                            "upload_job_id": 8,
                            "scheduled": False,
                            "reason": "profile_not_found",
                        },
                    ]
                }
            }
        }
    }

    lines = server_app._output_funnel_schedule_lines(handoff)

    assert lines == [
        "upload_job_id=7 publish_at=2026-05-22T09:00:00Z",
        "upload_job_id=8 schedule_failed=profile_not_found",
    ]


def test_get_job_debug_returns_compact_ai_summary(client):
    c, jobs_root = client
    job_id = "job_20260512T140000Z_abcdef12"
    _create_job(
        jobs_root,
        job_id=job_id,
        created_at="2026-05-12T14:00:00+00:00",
        with_debug_artifacts=True,
    )

    resp = c.get(f"/jobs/{job_id}/debug")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["job"]["job_id"] == job_id
    assert data["status"] == "success"
    assert data["stage_timings_ms"]["selection_ms"] == 123
    assert data["clips"][0]["title"] == "Useful Clip"
    assert data["clip_validation_issues"] == []
    assert data["transcript_stats"]["available"] is True
    assert data["transcript_stats"]["segment_count"] == 2
    assert data["transcript_stats"]["text_char_count"] == 5000
    assert "text" not in data["transcript_stats"]
    assert data["selection_summary"]["available"] is True
    assert data["selection_summary"]["clip_count"] == 1
    assert data["selection_summary"]["validation_warning_count"] == 1


def test_unknown_job_returns_404(client):
    c, _ = client
    resp = c.get("/jobs/job_20260512T150000Z_deadbeef")

    assert resp.status_code == 404
    assert resp.get_json()["success"] is False


@pytest.mark.parametrize(
    "unsafe_id",
    [
        "../report",
        "job_20260512T150000Z_deadbeef/../../x",
        "job_20260512T150000Z_DEADBEEF",
        "not-a-job",
    ],
)
def test_unsafe_job_ids_are_rejected(client, unsafe_id: str):
    c, _ = client
    resp = c.get(f"/jobs/{unsafe_id}")

    assert resp.status_code in (400, 404)
    data = resp.get_json()
    if data is not None:
        assert data["success"] is False


def test_process_resolves_input_id_and_updates_ledger(
    client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    c, jobs_root = client
    monkeypatch.setenv("INPUT_JOB_LEDGER_DIR", str(tmp_path / "input_jobs"))
    monkeypatch.setattr(input_paths, "SEEN_FILE", tmp_path / "seen_urls.json")
    monkeypatch.setattr(
        server_app,
        "resolve_http_policy_bundle",
        lambda **_: {"selection": {}, "policy_audit": {}, "models_effective": {}},
    )

    def fake_run_pipeline(
        video_path: str,
        policy_bundle: dict,
        *,
        input_id: str | None = None,
        job_id: str | None = None,
        worker_job: dict[str, str] | None = None,
    ):
        report = {
            "job_id": job_id,
            "input_id": input_id,
            "input_video_name": os.path.basename(video_path),
            "status": "success",
            "current_stage": "success",
            "created_at": server_app.now_iso(),
            "completed_at": server_app.now_iso(),
            "errors": [],
            "warnings": [],
            "stage_timings_ms": {},
            "clips": [{"clip_file": "clip_01.mp4"}],
        }
        if worker_job and worker_job.get("report_path"):
            Path(worker_job["report_path"]).write_text(json.dumps(report), encoding="utf-8")
        else:
            for path in jobs_root.glob(f"*_{job_id}/report.json"):
                path.write_text(json.dumps(report), encoding="utf-8")
        return server_app.jsonify(
            {
                "success": True,
                "pipeline": server_app.PIPELINE_NAME,
                "input_id": input_id,
                "job_id": job_id,
                "run_id": "run_1",
                "clips": [{"clip_file": "clip_01.mp4"}],
                "source_video": os.path.basename(video_path),
            }
        )

    monkeypatch.setattr(server_app, "_run_pipeline", fake_run_pipeline)
    video_path = tmp_path / "input.mp4"
    video_path.write_bytes(b"fake-video")
    record = input_ledger.create_record(
        funnel_id="business_podcasts_001",
        source_url="https://example.test/watch?v=abc",
        source_metadata={"video_id": "abc"},
    )
    input_ledger.mark_downloaded(record["input_id"], video_path)

    resp = c.post(
        "/process",
        json={
            "input_id": record["input_id"],
            "selection": {"max_clips": 1, "min_duration_sec": 10, "max_duration_sec": 30},
        },
    )

    assert resp.status_code == 202
    body = resp.get_json()
    assert body["success"] is True
    assert body["input_id"] == record["input_id"]
    deadline = time.time() + 3
    updated = input_ledger.load_record(record["input_id"])
    while updated["state"] != "succeeded" and time.time() < deadline:
        time.sleep(0.05)
        updated = input_ledger.load_record(record["input_id"])
    updated = input_ledger.load_record(record["input_id"])
    assert updated["state"] == "succeeded"
    assert updated["result"]["pipeline_job_id"] == body["job_id"]


def test_post_jobs_rejects_absolute_path_outside_input_root(client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    c, jobs_root = client
    monkeypatch.setattr(
        server_app,
        "resolve_http_policy_bundle",
        lambda **_: {"selection": {}, "policy_audit": {}, "models_effective": {}},
    )
    monkeypatch.setattr(server_app, "_enqueue_job", lambda task: None)
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"fake-video")

    resp = c.post("/jobs", json={"video_path": str(video_path)})

    assert resp.status_code == 400
    assert "configured input folder" in resp.get_json()["error"]
    assert list(jobs_root.glob("*/report.json")) == []


def test_post_jobs_creates_queued_job_from_input_folder_path(client, monkeypatch: pytest.MonkeyPatch):
    c, jobs_root = client
    monkeypatch.setattr(
        server_app,
        "resolve_http_policy_bundle",
        lambda **_: {"selection": {}, "policy_audit": {}, "models_effective": {}},
    )
    monkeypatch.setattr(server_app, "_enqueue_job", lambda task: None)
    input_root = Path(server_app.ensure_paths(server_app.load_config())["input"])
    video_path = input_root / "source.mp4"
    video_path.write_bytes(b"fake-video")

    resp = c.post("/jobs", json={"video_path": str(video_path)})

    assert resp.status_code == 202
    data = resp.get_json()
    assert data["status"] == "queued"
    assert data["status_url"] == f"/jobs/{data['job_id']}"
    reports = list(jobs_root.glob("*/report.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["job_id"] == data["job_id"]
    assert report["status"] == "queued"
    assert (reports[0].parent / "task.json").is_file()


def test_post_jobs_missing_input_writes_failed_report(client):
    c, jobs_root = client

    resp = c.post("/jobs", json={"video": "missing.mp4"})

    assert resp.status_code == 400
    data = resp.get_json()
    assert data["status"] == "failed"
    assert data["error"] == "Input video not found"
    reports = list(jobs_root.glob("*/report.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["errors"][0]["category"] == "input_video_not_found"


def test_recover_pending_jobs_requeues_disk_task(client, monkeypatch: pytest.MonkeyPatch):
    c, jobs_root = client
    input_root = Path(server_app.ensure_paths(server_app.load_config())["input"])
    video_path = input_root / "recover.mp4"
    video_path.write_bytes(b"fake-video")
    job_id = "job_20260522T120000Z_abc123ef"
    job_dir = jobs_root / f"recover_{job_id}"
    job_dir.mkdir(parents=True)
    report_path = job_dir / "report.json"
    task_path = job_dir / "task.json"
    review_path = job_dir / "review.md"
    job = {
        "job_id": job_id,
        "job_dir": str(job_dir),
        "clips_dir": str(job_dir / "clips"),
        "input_copy_path": str(job_dir / "input_recover.mp4"),
        "transcript_copy_path": str(job_dir / "transcript.json"),
        "normalized_transcript_path": str(job_dir / "transcript_payload.json"),
        "selection_path": str(job_dir / "selection.json"),
        "report_path": str(report_path),
        "task_path": str(task_path),
        "analytics_path": str(job_dir / "analytics.json"),
        "review_path": str(review_path),
    }
    _write_json(
        report_path,
        {
            "job_id": job_id,
            "input_video_name": "recover.mp4",
            "input_video_path": str(video_path),
            "status": "queued",
            "created_at": "2026-05-22T12:00:00+00:00",
            "errors": [],
            "warnings": [],
            "clips": [],
        },
    )
    _write_json(
        task_path,
        {
            "job_id": job_id,
            "job": job,
            "video_path": str(video_path),
            "input_id": None,
            "input_source": "input_folder",
            "policy_bundle": {"selection": {}, "policy_audit": {}, "models_effective": {}},
            "created_at": "2026-05-22T12:00:00+00:00",
        },
    )
    server_app._JOB_RECOVERY_DONE = False
    recovered: list[dict] = []
    monkeypatch.setattr(server_app._JOB_QUEUE, "put", lambda task: recovered.append(task))
    monkeypatch.setattr(server_app, "_ensure_job_workers_started", lambda: None)

    count = server_app._recover_pending_jobs_once()

    assert count == 1
    assert recovered[0]["job_id"] == job_id
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "queued"
    assert report["recovered_at"]


def test_sweep_stale_jobs_fails_old_running(client, monkeypatch: pytest.MonkeyPatch):
    c, jobs_root = client
    monkeypatch.delenv("MK04_ENV", raising=False)
    monkeypatch.setattr(server_app, "_jobs_root_readonly", lambda: str(jobs_root))
    job_id = "job_20260522T130000Z_abc123ef"
    job_dir = jobs_root / job_id
    job_dir.mkdir(parents=True)
    report_path = job_dir / "report.json"
    _write_json(
        report_path,
        {
            "job_id": job_id,
            "status": "running",
            "current_stage": "transcription",
            "started_at": "2020-01-01T00:00:00+00:00",
            "heartbeat_at": "2020-01-01T00:00:00+00:00",
            "errors": [],
            "warnings": [],
            "clips": [],
        },
    )
    monkeypatch.setenv("VIDEO_JOB_STALE_RUNNING_SEC", "60")

    count = server_app._sweep_stale_jobs()

    assert count == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["errors"][-1]["category"] == "stale_job"


def test_pipeline_job_paths_reuses_worker_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worker_job = {
        "job_id": "job_worker123",
        "job_dir": str(tmp_path / "jobs" / "dev" / "job_worker123"),
        "report_path": str(tmp_path / "jobs" / "dev" / "job_worker123" / "report.json"),
        "clips_dir": str(tmp_path / "jobs" / "dev" / "job_worker123" / "clips"),
    }
    create_calls: list[str] = []

    def _fail_create(*_args, **_kwargs):
        create_calls.append("create_job_paths")
        raise AssertionError("create_job_paths must not run when worker_job is provided")

    monkeypatch.setattr(server_app, "create_job_paths", _fail_create)
    resolved = server_app._pipeline_job_paths(
        {},
        "/tmp/input_example_source.mp4",
        job_id="job_worker123",
        worker_job=worker_job,
    )
    assert resolved is worker_job
    assert create_calls == []


def test_touch_job_heartbeat_updates_running_report(tmp_path: Path) -> None:
    job_id = "job_20260522T120000Z_abc123ef"
    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True)
    report_path = job_dir / "report.json"
    report = {
        "job_id": job_id,
        "status": "running",
        "current_stage": "transcription",
        "started_at": "2026-05-22T12:00:00+00:00",
        "errors": [],
        "warnings": [],
        "clips": [],
    }
    _write_json(report_path, report)
    job = {"job_id": job_id, "report_path": str(report_path)}

    server_app._touch_job_heartbeat(job, report, current_stage="selection")

    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "running"
    assert persisted["current_stage"] == "selection"
    assert persisted.get("heartbeat_at")
    assert report.get("heartbeat_at") == persisted["heartbeat_at"]


def test_touch_job_heartbeat_skips_non_running(tmp_path: Path) -> None:
    job_id = "job_20260522T120001Z_abc123ef"
    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True)
    report_path = job_dir / "report.json"
    report = {
        "job_id": job_id,
        "status": "success",
        "current_stage": "success",
        "errors": [],
        "warnings": [],
        "clips": [],
    }
    _write_json(report_path, report)
    job = {"job_id": job_id, "report_path": str(report_path)}

    server_app._touch_job_heartbeat(job, report, current_stage="clipping")

    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert "heartbeat_at" not in persisted
    assert persisted["current_stage"] == "success"


def test_sweep_stale_jobs_respects_fresh_heartbeat(client, monkeypatch: pytest.MonkeyPatch):
    c, jobs_root = client
    monkeypatch.delenv("MK04_ENV", raising=False)
    monkeypatch.setattr(server_app, "_jobs_root_readonly", lambda: str(jobs_root))
    job_id = "job_20260522T131500Z_abc123ef"
    job_dir = jobs_root / job_id
    job_dir.mkdir(parents=True)
    report_path = job_dir / "report.json"
    _write_json(
        report_path,
        {
            "job_id": job_id,
            "status": "running",
            "current_stage": "clipping",
            "started_at": "2020-01-01T00:00:00+00:00",
            "heartbeat_at": "2026-07-05T15:00:00+00:00",
            "errors": [],
            "warnings": [],
            "clips": [],
        },
    )
    monkeypatch.setenv("VIDEO_JOB_STALE_RUNNING_SEC", "60")
    fixed_now = server_app._timestamp_epoch("2026-07-05T15:00:30+00:00")
    assert fixed_now is not None
    monkeypatch.setattr(server_app.time, "time", lambda: fixed_now)

    count = server_app._sweep_stale_jobs()

    assert count == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "running"


def test_cancel_job_marks_running_failed(client, monkeypatch: pytest.MonkeyPatch):
    c, jobs_root = client
    monkeypatch.delenv("MK04_ENV", raising=False)
    monkeypatch.setattr(server_app, "_jobs_root_readonly", lambda: str(jobs_root))
    job_id = "job_20260522T140000Z_abc123ef"
    job_dir = jobs_root / job_id
    job_dir.mkdir(parents=True)
    report_path = job_dir / "report.json"
    _write_json(
        report_path,
        {
            "job_id": job_id,
            "status": "running",
            "current_stage": "transcription",
            "created_at": "2026-05-22T14:00:00+00:00",
            "errors": [],
            "warnings": [],
            "clips": [],
        },
    )
    (job_dir / "review.md").write_text("# review\n", encoding="utf-8")

    response = c.post(f"/jobs/{job_id}/cancel")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "failed"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["errors"][-1]["category"] == "operator_cancel"
