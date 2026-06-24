"""Stalled-job detection used by the autonomy watchdog.

A job is "stalled" if it has been sitting in an intermediate status
(``registered``, ``routed``, or ``uploading``) longer than configured
thresholds without progressing. The watchdog calls
``GET /admin/stalled-jobs`` every 15 minutes; a non-zero ``count`` is an
operator alarm.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from output_funnel.models import UploadStatus
from output_funnel.registry import register_job_payload
from output_funnel.service import find_stalled_jobs
from output_funnel.store import OutputStore


def _payload(clip_path: Path) -> dict[str, Any]:
    return {
        "job_id": "job_stalled_test",
        "status": "success",
        "clips": [
            {
                "clip_id": "clip_1",
                "start": "00:00:01.000",
                "end": "00:00:31.000",
                "duration_sec": 30.0,
                "job_clip_path": str(clip_path),
                "title": "Title",
                "hook": "Hook",
                "caption": "Caption",
                "clip_validation": {"ok": True},
                "funnel_id": "business_clips_test",
            }
        ],
        "enabled_platforms": ["youtube_shorts"],
    }


def _make_registered_store(monkeypatch, tmp_path: Path) -> tuple[OutputStore, int]:
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path))
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    job = store.get_upload_job(upload_job_id)
    assert job is not None and job["status"] == UploadStatus.REGISTERED
    return store, upload_job_id


def _backdate(store: OutputStore, upload_job_id: int, *, column: str, seconds: int) -> None:
    """Move a job's timestamp into the past so it appears stalled."""
    backdated = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    with store.connect() as conn:
        conn.execute(
            f"UPDATE upload_jobs SET {column} = ? WHERE id = ?",
            (backdated, upload_job_id),
        )


def test_find_stalled_jobs_returns_registered_row_older_than_threshold(monkeypatch, tmp_path: Path):
    store, upload_job_id = _make_registered_store(monkeypatch, tmp_path)
    _backdate(store, upload_job_id, column="updated_at", seconds=3600)

    settings = {"stalled_jobs": {"registered_seconds": 60, "routed_seconds": 60, "uploading_seconds": 60}}
    result = find_stalled_jobs(store=store, settings=settings)

    assert result["count"] == 1
    assert result["jobs"][0]["id"] == upload_job_id
    assert result["jobs"][0]["status"] == UploadStatus.REGISTERED


def test_find_stalled_jobs_ignores_fresh_rows(monkeypatch, tmp_path: Path):
    store, _ = _make_registered_store(monkeypatch, tmp_path)

    settings = {"stalled_jobs": {"registered_seconds": 60, "routed_seconds": 60, "uploading_seconds": 60}}
    result = find_stalled_jobs(store=store, settings=settings)

    assert result["count"] == 0
    assert result["jobs"] == []


def test_find_stalled_jobs_detects_stuck_uploading_via_upload_started_at(monkeypatch, tmp_path: Path):
    store, upload_job_id = _make_registered_store(monkeypatch, tmp_path)
    with store.connect() as conn:
        conn.execute(
            "UPDATE upload_jobs SET status = ? WHERE id = ?",
            (UploadStatus.UPLOADING, upload_job_id),
        )
    _backdate(store, upload_job_id, column="upload_started_at", seconds=7200)

    settings = {"stalled_jobs": {"uploading_seconds": 1800}}
    result = find_stalled_jobs(store=store, settings=settings)

    assert result["count"] == 1
    assert result["jobs"][0]["status"] == UploadStatus.UPLOADING


def test_find_stalled_jobs_threshold_zero_disables_check(monkeypatch, tmp_path: Path):
    store, upload_job_id = _make_registered_store(monkeypatch, tmp_path)
    _backdate(store, upload_job_id, column="updated_at", seconds=86400)

    settings = {"stalled_jobs": {"registered_seconds": 0, "routed_seconds": 0, "uploading_seconds": 0}}
    result = find_stalled_jobs(store=store, settings=settings)

    assert result["count"] == 0
