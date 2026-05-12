from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "server"))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import app as server_app  # noqa: E402


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
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
        },
    )
    monkeypatch.setenv("PIPELINE_CONFIG_PATH", str(cfg_path))
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


def test_get_job_returns_existing_report(client):
    c, jobs_root = client
    job_id = "job_20260512T130000Z_1234abcd"
    report = _create_job(jobs_root, job_id=job_id, created_at="2026-05-12T13:00:00+00:00")

    resp = c.get(f"/jobs/{job_id}")

    assert resp.status_code == 200
    assert resp.get_json() == report


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
